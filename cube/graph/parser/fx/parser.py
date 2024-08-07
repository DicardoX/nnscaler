# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import torch
import logging
from pathlib import Path
from typing import Any, List, Tuple, Callable, Union, Dict, Type, Optional

from cube.ir.operator import IRFwOperation
from cube.ir.tensor import IRFullTensor
from cube.ir.cten import IRObject, IRCell
from cube.graph.parser.frame import Frame
from cube.graph.parser.fx.mapping import SignFx2Op
from cube.graph.function.pyfunc import IRPyFunc
from cube.graph.function.dimops import IRDimops

import torch.fx
from .concrete_trace_utils import TensorMetadata

_logger = logging.getLogger(__name__)


class FxModuleParser:
    """
    torch.fx module parser
    """

    ATTR_CONTENT_FILE = 'fullmodel.pt'
    ATTR_MAP_FILE = 'dist_param_map.pt'

    @staticmethod
    def parse(module: torch.fx.GraphModule,
              dummy_inputs: Dict[str, Any],
              attr_savedir='./',
              *,
              save_content: bool = True,
              dynamic_shape: bool = True
        ) -> Tuple[List[IRObject], List[IRFwOperation], List[IRObject]]:
        """Parse torch.fx module into cube IR

        The overall entry to parse a torch.fx graph module

        Args:
            module (torch.fx.GraphModule): the torch.fx module
            dummy_inputs (Dict[str, Any]): the dummy inputs to run the module
            attr_savedir (str): the directory to save the attribute content
            save_content (bool): whether to save the content of the module
            dynamic_shape (bool): whether to parse the module with dynamic shape

        Returns:
            inputs (List[IRObject]): the input IRObjects
            all_ir_nodes (List[IRFwOperation]): the IRFwOperation nodes
            outputs (List[IRObject]): the output IRObjects
        """
        frame = Frame()
        frame.push_var()

        # shape propagation
        assert isinstance(dummy_inputs, dict), "Expected dummy inputs to parse module"

        # create IRObjects and IRTensors
        for node in module.graph.nodes:
            concrete_value = dummy_inputs.get(node.name) if node.op == 'placeholder' else None
            FxModuleParser.init_objects(node, module, frame, concrete_value)

        # get graph inputs
        placeholders = [n for n in module.graph.nodes if n.op == 'placeholder']
        inputs = [frame.get_var(n.name) for n in placeholders]
        # - if the graph inputs contain nested strcuture,
        #   it should be wrapped into an IRObject
        for idx, placeholder in enumerate(placeholders):
            if not isinstance(inputs[idx], IRObject):
                obj = IRObject(name=placeholder.name, value=inputs[idx])
                inputs[idx] = obj
                frame.set_var(placeholder.name, obj)

        # parse graph nodes
        all_ir_nodes = []
        for node in module.graph.nodes:
            ir_nodes = FxModuleParser.parse_node(node, module, dynamic_shape, frame)
            all_ir_nodes += ir_nodes
        
        # get graph outputs
        outputs = [frame.get_var(node.name) for node in module.graph.nodes if node.op == 'output']

        if save_content:
            attr_savedir = Path(attr_savedir)
            frame.save_attr_content(attr_savedir / FxModuleParser.ATTR_CONTENT_FILE)
            frame.save_attr_map(attr_savedir / FxModuleParser.ATTR_MAP_FILE)

        frame.pop_var()
        return inputs, all_ir_nodes, outputs

    @staticmethod
    def parse_node(node: torch.fx.Node, module, dynamic_shape: bool, frame: Frame) -> List[IRFwOperation]:
        """
        Parse the node and return the IRFwOperation nodes
        """
        if node.op == 'placeholder':
            return []
        if node.op == 'output':
            return FxModuleParser.parse_prim_output_node(node, module, frame)
        if node.op in ('call_function', 'call_method'):
            return FxModuleParser.parse_prim_function_method(node, module, dynamic_shape, frame)
        if node.op == 'get_attr':
            return FxModuleParser.parse_prim_get_attr_node(node, module, frame)
        if node.op == 'call_module':
            return FxModuleParser.parse_prim_module(node, module, frame)
        else:
            raise TypeError(f"Unknown node kind {node.op}")

    @staticmethod
    def init_objects(node: torch.fx.Node, module: torch.fx.GraphModule,
                     frame: Frame, concrete_value: Optional[Any] = None):
        assert isinstance(node, torch.fx.Node)

        def meta2var(meta: Any) -> Any:
            """Support complex data type of List, Tuple, Dict, Tensor/Object"""
            if isinstance(meta, TensorMetadata):
                shape = meta.shape
                # TODO: support scalar type
                shape = torch.Size([1]) if shape == torch.Size([]) else shape
                dtype = meta.dtype
                requires_grad = meta.requires_grad
                return IRFullTensor(shape=shape, name=node.name,
                                    requires_grad=requires_grad, dtype=dtype)
            if isinstance(meta, list):
                return list(meta2var(item) for item in meta)
            if isinstance(meta, tuple):
                return tuple(meta2var(item) for item in meta)
            if isinstance(meta, dict):
                if not all(isinstance(key, str) for key in meta.keys()):
                    raise TypeError(f"only support dict type with str key, but got {meta.keys()}.\n{node}")
                return {key : meta2var(value) for key, value in meta.items()}
            # TODO: data type check, with cases like {'a': 1.2, 'b': torch.Tensor}
            return meta

        if hasattr(node, 'meta') and node.meta.get('tensor_meta'):
            meta = node.meta['tensor_meta']
            val = meta2var(meta)
        else:
            # FIXME: double check: there should be a concrete value as example,
            # otherwise, it may fail in parsing node like getattr
            val = IRObject(name=node.name, value=concrete_value)
        
        frame.add_var(node.name, val)

    @staticmethod
    def parse_complex(val: Any, frame: Frame) -> Any:
        """parse complex fx.Node into IRObject
        
        The val is usually from a node's input or output, can be fx.Node nested
        by tuple/list/dict type, or a fx.Node itself.

        Args:
            val (Any): fx.Node nested by tuple/list/dict
            frame (Frame): the frame to get the fx.Node

        Returns:
            the copied strcuture where the fx.Node is replaced by IRObjects/IRTensors
        """
        # to support more nested types, we can refer to the implementation of
        # https://github.com/pytorch/pytorch/blob/main/torch/utils/_pytree.py
        if isinstance(val, tuple):
            return tuple(FxModuleParser.parse_complex(t, frame) for t in val)
        if isinstance(val, list):
            return list(FxModuleParser.parse_complex(t, frame) for t in val)
        if isinstance(val, dict):
            return {key: FxModuleParser.parse_complex(val, frame) for key, val in val.items()}
        if isinstance(val, torch.fx.Node):
            return frame.get_var(val.name)
        return val

    @staticmethod
    def fetch_attr(mod: torch.fx.GraphModule, target: str):
        target_atoms = target.split('.')
        attr_itr = mod
        for i, atom in enumerate(target_atoms):
            if not hasattr(attr_itr, atom):
                raise RuntimeError(f"Node referenced nonexistent target {'.'.join(target_atoms[:i])}")
            attr_itr = getattr(attr_itr, atom)
        return attr_itr

    @staticmethod
    def parse_prim_module(node: torch.fx.Node, module: torch.fx.GraphModule, frame: Frame) -> List[IRFwOperation]:
        prim_module = FxModuleParser.fetch_attr(module, node.target)
        if prim_module.__class__.__module__.startswith('torch.nn.modules'):
            raise RuntimeError(f'{prim_module.__class__.__module__} can not be parsed as leaf nodes')
        else:
            raise RuntimeError(f'unknown module: {prim_module.__class__.__module__}')

    @staticmethod
    def parse_prim_function_method(node: torch.fx.Node, module: torch.fx.GraphModule, dynamic_shape: bool, frame: Frame) -> List[IRFwOperation]:
        # get signature
        fsig = FxModuleParser._get_qualified_name(node.target, node)
        # get inputs
        input_vals = FxModuleParser.parse_complex(list(node.args), frame)
        kwargs = FxModuleParser.parse_complex(node.kwargs, frame)

        if SignFx2Op.exist(fsig):
            ir_node = SignFx2Op.map(fsig)(*input_vals, **kwargs)
        else:
            # FIXME: handle cases for IRObject in kwargs
            # case1: unknown torch operator
            if FxModuleParser._is_torch_autograd_op(node, frame, fsig):
                _logger.warning(f'Find unknown pytorch operation: {fsig}')
                fname = fsig.split('.')[-1] if '.' in fsig else fsig
                ir_node = IRFwOperation(fname, fsig, input_vals, 1, **kwargs)
            # case2: python runtime function
            else:
                _logger.warning(f'Set python runtime function: {fsig}')
                ir_node = IRPyFunc(fsig, input_vals, [IRObject()], **kwargs)

        if isinstance(ir_node, IRCell):
            comment = str(node.meta.get('frame_record', ''))
            if comment:
                ir_node.comment = comment

        ir_nodes = []
        if isinstance(ir_node, IRCell):
            ir_nodes.append(ir_node)
            if len(ir_node.outputs()) > 1:
                vals = frame.get_var(node.name)
                assert len(vals) == len(ir_node.outputs()), f'{vals}, {ir_node.outputs()}'
                for i in range(len(vals)):
                    ir_node.set_output(i, vals[i])
            elif ir_node.output(0).value is not None:
                if dynamic_shape:
                    frame.set_var(node.name, ir_node.output(0))
                    ir_node.output(0).name = node.name
                else:
                    # if use static shape graph, all IRObject will be converted to real traced value.
                    # the ir_node will be folded and not appeared in the final graph
                    frame.set_var(node.name, ir_node.output(0).value)
                    ir_nodes.pop(-1)
            else:
                output_val = frame.get_var(node.name)
                if isinstance(ir_node, IRDimops):
                    ir_node.infer_shape()
                    assert output_val.shape == ir_node.output(0).shape, (
                        f'find shape inference not match: {output_val.shape} vs {ir_node.output(0).shape}'
                        f'\nnode: {node}'
                    )
                ir_node.set_output(0, output_val)
        else:
            frame.set_var(node.name, ir_node)

        _logger.info(f'parsing result: {ir_node}')
        return ir_nodes

    @staticmethod
    def parse_prim_get_attr_node(node: torch.fx.Node, module: torch.fx.GraphModule, frame: Frame) -> List[IRFwOperation]:
        """
        There are two types of get_attr, one is `FxNodeKind.PrimGetAttr` which is dealt with in this function.
        The other is `FxNodeKind.PrimCallFunction ` (i.e., <built-in function getattr>)
        which is dealt with by parse_prim_function_method.
        """
        concrete_value = FxModuleParser.fetch_attr(module, node.target)
        if isinstance(concrete_value, torch.Tensor):
            assert isinstance(concrete_value, torch.Tensor), \
                f"GetAttrPrim: expect tensor but got {type(concrete_value)}"
            exist_tensor = frame.get_attr_var(concrete_value)
            # the case that the parameter is the first time used by getattr
            if not exist_tensor:
                tensor = frame.get_var(node.name)
                if tensor.requires_grad:
                    tensor.as_param()
                else:
                    tensor.as_buffer()
                frame.add_attr(tensor, concrete_value, node.target)
            # the case that the parameter is consumed multiple times and regisetered previously
            else:
                frame.set_var(node.name, exist_tensor)            
        else:
            assert not isinstance(concrete_value, torch.Tensor), f"GetAttrPrim: unexpected parameter"
            frame.set_var(node.name, concrete_value)
        return []

    @staticmethod
    def parse_prim_output_node(node: torch.fx.Node, module: torch.fx.GraphModule, frame: Frame) -> List[IRCell]:
        assert len(node.args) == 1 and len(node.kwargs) == 0
        output = FxModuleParser.parse_complex(node.args[0], frame)
        frame.set_var(node.name, output)
        return []

    @staticmethod
    def _get_qualified_name(node_target: Union[str, Callable[..., Any]], node: torch.fx.Node = None) -> str:
        if isinstance(node_target, str):
            assert node is not None
            return FxModuleParser._get_qualified_name_of_call_method(node_target, node)
        else:
            return FxModuleParser._get_qualified_name_of_call_function(node_target)

    @staticmethod
    def _get_qualified_name_of_call_function(node_target: Callable[..., Any]) -> str:
        """
        The target field of call_function node must be an callable object.
        """
        # # things like getattr just appear in builtins
        # if getattr(builtins, func.__name__, None) is func:
        #     return func.__name__
        # TODO(yizhu1): find a general solution
        assert callable(node_target)
        name = node_target.__name__
        module = FxModuleParser._find_module_of_method(node_target)
        module = module.replace('torch._ops', 'torch.ops')  # WAR for bug in how torch.ops assigns module
        return f'{module}.{name}'

    @staticmethod
    def _get_qualified_name_of_call_method(node_target: str, node: torch.fx.Node) -> str:
        """
        The target field of call_method node must be a string.
        """
        assert isinstance(node_target, str)
        for module, module_name in [(torch, 'torch'), (torch.Tensor, 'torch.Tensor')]:
            lib_func = getattr(module, node_target, None)
            if lib_func is not None and callable(lib_func):
                return f'{module_name}.{node_target}'
        assert len(node.args) == 1, f'invalid args {node.args} in {node.name}, {node.target}, {node.meta}'
        assert len(node.kwargs) == 0, f'invalid kwargs {node.kwargs} in {node.name}, {node.target}, {node.meta}'
        # example node.args[0].meta is {'type': <class 'dict'>}
        in_type = node.args[0].meta['type']
        assert node_target in in_type().__dir__(), f'node_target = {node_target}, in_type().__dir__() = {in_type().__dir__()}'
        sig = f'{in_type.__name__}.{node_target}'
        return sig

    @staticmethod
    def _find_module_of_method(orig_method: Callable[..., Any]) -> str:
        if getattr(orig_method, '__name__', None) == 'apply' and isinstance(getattr(orig_method, '__self__', None), Type) \
            and issubclass(orig_method.__self__, torch.autograd.Function):
            # for torch.autograd.Function
            return f'{orig_method.__self__.__module__}.{orig_method.__self__.__name__}'

        name = orig_method.__name__
        module = orig_method.__module__
        # if hasattr(orig_method, '__qualname__') and isinstance(orig_method.__qualname__, str):
        #     # if there has '.' in '__qualname__', it means this function is in a nested structure,
        #     #
        #     # for example, it is a method / function in a class:
        #     # torch.nn.Linear.forward.__module__ = torch.nn
        #     # torch.nn.Linear.forward.__name__ = forward
        #     # torch.nn.Linear.forward.__qualname__ = Linear.forward
        #     #
        #     # And in fx.node qualified name creating rule, the module also should include the class name,
        #     # in this example, the returned module should be `torch.nn.Linear`.
        #     splited_names = orig_method.__qualname__.split('.')
        #     class_name, name = splited_names[:-1], splited_names[-1]
        #     module = '.'.join([module] + class_name)
        if module == 'torch.autograd.grad_mode' and name in ['__enter__', '__exit__']:
            return 'torch.autograd.grad_mode.no_grad'
        if module is not None:
            return module
        if hasattr(orig_method, '__qualname__')\
            and isinstance(orig_method.__qualname__, str) and orig_method.__qualname__.startswith('_VariableFunctionsClass.'):
            return 'torch._C._VariableFunctions'
        for guess in [torch, getattr(torch.nn, 'functional')]:
            if getattr(guess, name, None) is orig_method:
                return guess.__name__
        raise RuntimeError(f'cannot find module for {orig_method}')

    @staticmethod
    def _is_torch_autograd_op(node: torch.fx.Node, frame: Frame, signature: str) -> bool:
        """Check whether the node is of a pytorch autograd operation."""
        # note: some python operations like torch.Tensor.size() doesn't return
        # an IRTensor, thus cannot be considered as a pytorch autograd operator.
        return signature.startswith('torch.') and \
               isinstance(frame.get_var(node.name), IRFullTensor)
