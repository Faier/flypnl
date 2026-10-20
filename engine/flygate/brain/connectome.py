"""MaleCNS v1.0 真实连接组导入（真接线）。

数据源（公开 GCS，无需账号/令牌）：
  body-annotations-*.feather   211,577 条目: bodyId/type/superclass/status...
  body-neurotransmitters-*.feather  每个 bodyId 的 consensus_nt
  connectome-weights-*.feather 记录批: body_pre / body_post / weight(突触接触数)

收录策略与 StonkFly (MIT) 一致：保留有 superclass 注释的条目、剔除显式 Glia；
保留全部已发布边（含弱边与自环），不做阈值裁剪。导入后应复现官方
166,700 神经元 / 25,582,938 边 / 124,177,617 突触接触。

本模块只在 load 时才需要 pandas/pyarrow（纯 stdlib+numpy 的其他代码不受影响）。
"""
from __future__ import annotations

import os
import urllib.request

import numpy as np

BASE = ("https://storage.googleapis.com/flyem-male-cns/v1.0/"
        "connectome-data/flat-connectome")
FILES = {
    "annotations": "body-annotations-male-cns-v1.0-minconf-0.5.feather",
    "neurotransmitters": "body-neurotransmitters-male-cns-v1.0.feather",
    "edges": "connectome-weights-male-cns-v1.0-minconf-0.5.feather",
}
# 官方 v1.0 全量数字（导入正确性的硬校验）
EXPECTED = {"nodes": 166_700, "edges": 25_582_938, "contacts": 124_177_617}


def data_dir() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return os.path.join(here, "data", "connectome")


def download(names: tuple = ("annotations", "neurotransmitters", "edges"),
             dest: str | None = None) -> None:
    """下载原始 feather（断点续传；约 1GB，edges 那个最大）。

    断点续传用 Range 请求：已存在且 < 完整尺寸时从已有字节继续。
    """
    d = dest or data_dir()
    os.makedirs(d, exist_ok=True)
    for name in names:
        fname, url = FILES[name], f"{BASE}/{FILES[name]}"
        path = os.path.join(d, fname)
        total = int(_head_content_length(url) or 0)
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if total and have >= total:
            print(f"[connectome] {fname}: already complete ({have} bytes)")
            continue
        headers = {"Range": f"bytes={have}-"} if have else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as r, open(path, "ab" if have else "wb") as f:
            while True:
                chunk = r.read(1 << 22)
                if not chunk:
                    break
                f.write(chunk)
        print(f"[connectome] {fname}: {os.path.getsize(path)} bytes")


def _head_content_length(url: str) -> int | None:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req) as r:
            return int(r.headers.get("Content-Length") or 0)
    except OSError:
        return None


def _feather(path: str):
    import pandas as pd  # 惰性：只有真接线分析需要 pandas/pyarrow
    return pd.read_feather(path)


def load_nodes(dest: str | None = None):
    """返回 (catalog 全表, neurons 收录表)；收录策略见模块 docstring。"""
    d = dest or data_dir()
    a = _feather(os.path.join(d, FILES["annotations"]))
    nt = _feather(os.path.join(d, FILES["neurotransmitters"])).set_index("body")
    retain = (a["superclass"].notna() & a["superclass"].astype(str).ne("")
              & ~a["status"].eq("Glia"))
    neurons = a[retain].copy()
    neurons["nt"] = neurons["bodyId"].map(nt["consensus_nt"])
    neurons = neurons.sort_values("bodyId").reset_index(drop=True)
    neurons["node_index"] = np.arange(len(neurons), dtype=np.uint32)
    return a, neurons


def load_edges(dest: str | None = None):
    """按收录后的 node_index 重映射全部边，返回 (pre, post, weight) uint32 数组。"""
    import pyarrow.ipc as ipc
    import pyarrow as pa

    d = dest or data_dir()
    _, neurons = load_nodes(d)
    ids = neurons["bodyId"].to_numpy(dtype=np.uint64)
    path = os.path.join(d, FILES["edges"])
    reader = ipc.open_file(pa.memory_map(path, "r"))
    pre_list, post_list, w_list = [], [], []
    for b in range(reader.num_record_batches):
        batch = reader.get_batch(b)
        pre = batch.column(batch.schema.get_field_index("body_pre")) \
            .to_numpy(zero_copy_only=False)
        post = batch.column(batch.schema.get_field_index("body_post")) \
            .to_numpy(zero_copy_only=False)
        w = batch.column(batch.schema.get_field_index("weight")) \
            .to_numpy(zero_copy_only=False)
        i = np.searchsorted(ids, pre.astype(np.uint64))
        j = np.searchsorted(ids, post.astype(np.uint64))
        keep = (i < len(ids)) & (j < len(ids))
        keep &= ids[np.minimum(i, len(ids) - 1)] == pre.astype(np.uint64)
        keep &= ids[np.minimum(j, len(ids) - 1)] == post.astype(np.uint64)
        pre_list.append(i[keep].astype(np.uint32))
        post_list.append(j[keep].astype(np.uint32))
        w_list.append(w[keep].astype(np.uint32))
    return (np.concatenate(pre_list), np.concatenate(post_list),
            np.concatenate(w_list))


def type_array(neurons) -> np.ndarray:
    """type 列 -> numpy unicode 数组（pandas 3 的 str 扩展 dtype 不能直接
    用于 np.char/比较，先过一遍 astype(str) 再落成 '<U'）。"""
    return np.asarray([str(x) for x in neurons["type"]], dtype=str)


def cell_index(neurons, pattern: str) -> np.ndarray:
    """按 type 字段正则挑细胞，返回 node_index。"""
    m = neurons["type"].astype(str).str.contains(pattern, na=False,
                                                 case=False, regex=True)
    return neurons.loc[m, "node_index"].to_numpy(dtype=np.uint32)


def graph_summary(pre: np.ndarray, post: np.ndarray, w: np.ndarray) -> dict:
    return {"nodes": int(len(np.unique(np.concatenate([pre, post])))),
            "edges": int(len(pre)), "contacts": int(w.sum(dtype=np.uint64))}
