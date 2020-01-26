import torch
import tvm
import numpy as np
from tvm import relay
from tvm.relay import expr as _expr
from tvm.relay.frontend.common import infer_shape


class QuantParam:
    def __init__(self, weight, scale, zero_point, param_key):
        param_prefix = param_key[:-len("._packed_params")]
        self.weight_var = _expr.var(param_prefix + "_weight",
                                    shape=weight.shape)
        self.weight = weight
        self.scale = _expr.const(np.asscalar(scale))
        self.zero_point = _expr.const(np.asscalar(zero_point),
                                      dtype="int32")


def unpack_quant_params(param_name, packed_params):
    if "fc" in param_name:
        qweight, bias = torch.ops.quantized.linear_unpack(packed_params)
    else:
        qweight, bias = torch.ops.quantized.conv2d_unpack(packed_params)

    weight = qweight.dequantize().numpy()
    if qweight.qscheme() == torch.per_tensor_affine:
        scale = np.array([qweight.q_scale()])
        zero_point = np.array([qweight.q_zero_point()], dtype="int32")
        param = QuantParam(weight, scale, zero_point, param_name)
    else:
        scales = qweight.q_per_channel_scales().numpy()
        zero_points = qweight.q_per_channel_zero_points().numpy()
        param = QuantParam(weight, scales, zero_points, param_name)

    return param


def get_weight_quant_params(state_dict):
    quant_params = {}
    for key, value in state_dict.items():
        if key.endswith("_packed_params"):
            quant_params[key] = unpack_quant_params(key, value)
    return quant_params


def get_input_quant_param(state_dict):
    input_scale = state_dict["quant.scale"]
    input_zero_point = state_dict["quant.zero_point"]
    return 1.0 / float(input_scale[0]), int(input_zero_point[0])


def add_quant_params_to_outputs(outputs, name_map,
                                packed_param_map, quant_params):
    for node_name, packed_param_name in packed_param_map.items():
        qparam = quant_params[packed_param_name]
        name_map[node_name] = len(outputs)
        qweight = relay.qnn.op.quantize(qparam.weight_var, qparam.scale,
                                        qparam.zero_point, out_dtype="uint8")
        outputs.append((qweight, qparam.scale, qparam.zero_point))


def add_input_quant_params(op_name, inputs, input_scale, input_zero_point):
    needs_input_quant_param = ["quantized::conv2d", "quantized::conv2d_relu",
                               "aten::dequantize"]
    if op_name in needs_input_quant_param:
        inputs.append(relay.const(input_scale))
        inputs.append(relay.const(input_zero_point))


def add_quant_params(params, quant_params):
    for qparam in quant_params.values():
        params[qparam.weight_var.name_hint] = tvm.nd.array(qparam.weight)


def _quantize_per_tensor():
    def _impl(inputs, input_type):
        return relay.qnn.op.quantize(inputs[0], _expr.const(inputs[1]),
                                     _expr.const(inputs[2]), out_dtype="uint8",
                                     axis=1)
    return _impl


def _dequantize():
    def _impl(inputs, input_type):
        return relay.qnn.op.dequantize(inputs[0], inputs[1], inputs[2])
    return _impl


def _quantized_conv2d(with_relu=False):
    def _impl(inputs, input_type):
        # refer to src/ATen/native/quantized/cpu/qconv.cpp
        # inputs[0]: input tensor
        # inputs[1]: (weight, scale, zero_point)
        # inputs[2-5]: stride, padding, dilation, groups
        # inputs[6]: output_scale
        # inputs[7]: output_zero_point
        # inputs[8]: input_scale
        # inputs[9]: input_zero_point
        strides, padding, dilation = inputs[2], inputs[3], inputs[4]
        assert isinstance(strides, _expr.Var)
        strides = infer_shape(strides)
        assert isinstance(padding, _expr.Var)
        padding = infer_shape(padding)
        assert isinstance(dilation, _expr.Var)
        dilation = infer_shape(dilation)
        groups = inputs[5]
        # print(strides, padding, dilation, groups)

        weight = inputs[1][0]
        weight_scale = inputs[1][1]
        weight_zero_point = inputs[1][2]

        output_scale = _expr.const(inputs[6])
        output_zero_point = _expr.const(inputs[7])
        # print("output_scale, output_zero_point:", output_scale, output_zero_point)
        input_scale = inputs[8]
        input_zero_point = inputs[9]

        # print("input_scale, input_zero_point:", input_scale, input_zero_point)
        # print("weight_scale, weight_zero_point:", weight_scale, weight_zero_point)

        conv_out = relay.qnn.op.conv2d(inputs[0], weight,
                                       input_zero_point, weight_zero_point,
                                       input_scale, weight_scale,
                                       padding=(1, 1), kernel_size=(3, 3))

        requantized = relay.qnn.op.requantize(conv_out,
                                              input_scale, input_zero_point,
                                              output_scale, output_zero_point,
                                              out_dtype="uint8",
                                              axis=1)
        if with_relu:
            return relay.nn.relu(requantized)

        return requantized

    return _impl


convert_map = {
    'aten::quantize_per_tensor': _quantize_per_tensor(),
    'quantized::conv2d_relu': _quantized_conv2d(True),
    'aten::dequantize': _dequantize(),
    'quantized::conv2d': _quantized_conv2d(),
}
