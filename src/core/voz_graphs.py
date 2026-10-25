"""One-time rewrites of the Voz ONNX graphs for desktop CPU inference.

The published graphs target the official SDKs' GPU / Neural Engine batching:
the encoder is fixed to 15 s (188 encoder frames), the decoder to 16 lanes, and
both compute in float16. On a desktop CPU that means a 3 s dictation pays for
15 s of encoder work, a single window pays for 16 decoder lanes, and every op
converts float16 back and forth. All three are graph constants, so:

* Encoder length: each rel-pos attention layer multiplies by a precomputed
  positional table [1, heads, d_k, 2T] and then does the Transformer-XL shift
  with the constants 2T and 2T-1. For T' <= T frames the right table is the
  centre slice [T-T', T+T'), so the table becomes a Slice driven by the runtime
  length and the constants become 2T' and 2T'-1.
* Decoder lanes: two Reshapes hard-code 16 lanes; they become -1.
* Precision: float16 tensors, Casts and Constants become float32. The 4-bit
  MatMulNBits weights are not touched and are read from the original file, so
  only the converted tensors (about 115 MB) are written out.

Measured on a Ryzen 9 5950X: a 3.3 s clip went from 0.44 s to 0.11 s and an
8 s clip from 0.51 s to 0.18 s, with the same transcripts.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

ENCODER_CPU = "encoder.cpu.onnx"
ENCODER_CPU_DATA = "encoder.cpu.data"
DECODER_CPU = "decoder.cpu.onnx"
GRAPH_VERSION = "2"  # bump when a rewrite changes, so cached graphs rebuild
_VERSION_KEY = "whisper_anywhere_graph"


def ensure_cpu_graphs(folder: Path) -> tuple[Path, Path]:
    encoder = folder / ENCODER_CPU
    decoder = folder / DECODER_CPU
    if not _current(encoder):
        model = _dynamic_encoder(folder / "encoder.onnx")
        _to_float32(model, folder, ENCODER_CPU_DATA)
        _write(encoder, model)
    if not _current(decoder):
        import onnx

        model = _dynamic_decoder(onnx.load(str(folder / "decoder.onnx")))
        _to_float32(model, folder, None)
        _write(decoder, model)
    return encoder, decoder


def _current(path: Path) -> bool:
    if not path.is_file():
        return False
    import onnx

    try:
        model = onnx.load(str(path), load_external_data=False)
    except Exception:
        return False
    return any(
        prop.key == _VERSION_KEY and prop.value == GRAPH_VERSION
        for prop in model.metadata_props
    )


def _write(path: Path, model) -> None:
    import onnx

    entry = model.metadata_props.add()
    entry.key = _VERSION_KEY
    entry.value = GRAPH_VERSION
    temporary = path.with_name(path.name + ".tmp")
    onnx.save(model, str(temporary))
    temporary.replace(path)


def _int64(name: str, value):
    from onnx import helper, numpy_helper

    return helper.make_node(
        "Constant",
        [],
        [name],
        value=numpy_helper.from_array(np.array(value, dtype=np.int64), name),
    )


def _dynamic_encoder(source: Path):
    import onnx
    from onnx import helper, numpy_helper

    model = onnx.load(str(source), load_external_data=False)
    graph = model.graph
    initializers = {t.name: t for t in graph.initializer}
    patched = 0
    nodes = []
    for node in graph.node:
        output = node.output[0]
        attn = output.rsplit("/", 1)[0]
        # Every self_attn block reads its sequence length from Gather_1.
        length = f"{attn}/Gather_1_output_0"
        table = initializers.get(node.input[1]) if len(node.input) > 1 else None
        if (
            node.op_type == "MatMul"
            and "self_attn" in output
            and table is not None
            and len(table.dims) == 4
        ):
            full = table.dims[3] // 2
            tag = f"{attn}/wa_pos"
            nodes += [
                _int64(f"{tag}/full", [full]),
                _int64(f"{tag}/axis", [3]),
                _int64(f"{tag}/axes", [0]),
                helper.make_node("Unsqueeze", [length, f"{tag}/axes"], [f"{tag}/t"]),
                helper.make_node("Sub", [f"{tag}/full", f"{tag}/t"], [f"{tag}/start"]),
                helper.make_node("Add", [f"{tag}/full", f"{tag}/t"], [f"{tag}/end"]),
                helper.make_node(
                    "Slice",
                    [node.input[1], f"{tag}/start", f"{tag}/end", f"{tag}/axis"],
                    [f"{tag}/table"],
                ),
            ]
            node.input[1] = f"{tag}/table"
            nodes.append(node)
            patched += 1
            continue
        if node.op_type == "Constant" and "self_attn/Constant" in output:
            value = numpy_helper.to_array(node.attribute[0].t)
            tag = f"{output}/wa"
            if value.shape == (1,) and int(value[0]) in (376, 375):
                minus = 1 if int(value[0]) == 375 else 0
                nodes += [
                    _int64(f"{tag}/axes", [0]),
                    _int64(f"{tag}/two", [2]),
                    _int64(f"{tag}/minus", [minus]),
                    helper.make_node("Unsqueeze", [length, f"{tag}/axes"], [f"{tag}/t"]),
                    helper.make_node("Mul", [f"{tag}/t", f"{tag}/two"], [f"{tag}/2t"]),
                    helper.make_node("Sub", [f"{tag}/2t", f"{tag}/minus"], [output]),
                ]
                continue
            if value.shape == () and int(value) == 376:
                nodes += [
                    _int64(f"{tag}/two", 2),
                    helper.make_node("Mul", [length, f"{tag}/two"], [output]),
                ]
                continue
        nodes.append(node)
    if patched == 0:
        raise RuntimeError("The Voz encoder layout changed: no positional tables found.")
    del graph.node[:]
    graph.node.extend(nodes)
    for value, dim, label in (
        (graph.input[0], 3, "rows"),
        (graph.input[1], 3, "mel_frames"),
        (graph.input[2], 1, "frames"),
    ):
        value.type.tensor_type.shape.dim[dim].dim_param = label
    return model


def _dynamic_decoder(model):
    from onnx import numpy_helper

    graph = model.graph
    for node in graph.node:
        if node.op_type != "Constant":
            continue
        value = numpy_helper.to_array(node.attribute[0].t).tolist()
        if value == [32, -1]:  # [16 lanes * 2 frames, joint hidden]
            replacement = [-1, 640]
        elif value == [16, 2, 1]:
            replacement = [-1, 2, 1]
        else:
            continue
        node.attribute[0].t.CopyFrom(
            numpy_helper.from_array(np.array(replacement, np.int64), node.output[0])
        )
    for value in list(graph.input) + list(graph.output):
        value.type.tensor_type.shape.dim[0].dim_param = "lanes"
    return model


def _to_float32(model, folder: Path, data_name: str | None) -> None:
    """Turn every float16 tensor and cast into float32 in place.

    External float16 tensors are rewritten into `data_name`; everything else
    external (the 4-bit weights) keeps pointing at its original file.
    """
    import onnx
    from onnx import numpy_helper

    fp16 = onnx.TensorProto.FLOAT16
    fp32 = onnx.TensorProto.FLOAT
    sink = None
    if data_name is not None:
        sink = (folder / data_name).open("wb")  # fresh file: never append
    try:
        for tensor in model.graph.initializer:
            if tensor.data_type != fp16:
                continue
            if tensor.data_location == onnx.TensorProto.EXTERNAL:
                info = {entry.key: entry.value for entry in tensor.external_data}
                with (folder / info["location"]).open("rb") as source:
                    source.seek(int(info.get("offset", 0)))
                    raw = source.read(int(info["length"]))
                data = np.frombuffer(raw, np.float16).astype(np.float32).tobytes()
                if sink is None:
                    raise RuntimeError(f"No data file for external tensor {tensor.name}")
                offset = sink.tell()
                sink.write(data)
                tensor.data_type = fp32
                del tensor.external_data[:]
                for key, value in (
                    ("location", data_name),
                    ("offset", str(offset)),
                    ("length", str(len(data))),
                ):
                    entry = tensor.external_data.add()
                    entry.key = key
                    entry.value = value
                continue
            array = numpy_helper.to_array(tensor).astype(np.float32)
            tensor.CopyFrom(numpy_helper.from_array(array, tensor.name))
    finally:
        if sink is not None:
            sink.close()
    for node in model.graph.node:
        for attribute in node.attribute:
            if node.op_type == "Cast" and attribute.name == "to" and attribute.i == fp16:
                attribute.i = fp32
            elif attribute.type == onnx.AttributeProto.TENSOR and attribute.t.data_type == fp16:
                array = numpy_helper.to_array(attribute.t).astype(np.float32)
                attribute.t.CopyFrom(numpy_helper.from_array(array, attribute.t.name))
