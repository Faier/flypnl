"""C 阶段：MaleCNS 真实接线蘑菇体子图（冻结突触）。

把 flybrain.py 的两个合成部件换成真实连接组结构，其余（图表前端、LIF 常数、
DN 读出、决策门）一字不动：

  合成                                  ->  真实（本模块）
  ------------------------------------------------------------------
  kc_mask: 800 KC × 38 特征随机二值接线  ->  M[N_real_kc, n_feat]：
        每 KC 的真实外源输入接触数（1,649 个外源细胞 -> 3,679 个真实 KC，
        49,871 条边 / 470,191 接触），外源细胞按 bodyId 排序后轮转分配到
        特征通道（确定性，无随机数）
  w_a/w_v: 全体 KC -> 2 个 MBON 的全连接  ->  真实 KC->MBON07/MBON11 边
        （3,651 + 4,184 条边），权重 = 接触数按目标组 max 归一；
        证据 a_ev/v_ev 改为加权平均（除以权重质量和，保持 [0,1] 量纲）

诚实声明（同 StonkFly 的 display adapter）：
  * 特征通道 -> 外源细胞 的指派是工程约定：连接组不告诉我们哪根 K 线喂哪个 PN。
  * MBON07/MBON11 -> 接近/回避 的角色指派沿用 flybrain 的既有约定
    （Huang/Luo 2024 的 α1 与 γ1pedc 小室），未在本雄性重建中验证。
  * 本臂冻结突触（C 阶段）：多巴胺不更新任何权重；D 阶段才把学习限制在
    真实边上。

数据缓存：构建结果写入 data/connectome/realmb_f{n_feat}.npz（几百 KB），
FlyBrain 运行路径只读缓存，不需要 pandas/pyarrow。
"""
from __future__ import annotations

import os

import numpy as np

from . import connectome as C


def _cache_path(n_feat: int) -> str:
    return os.path.join(C.data_dir(), f"realmb_f{n_feat}.npz")


def _assign_features(cell_ids: np.ndarray, n_feat: int) -> np.ndarray:
    """外源细胞 -> 特征通道：按 bodyId 排序后轮转指派（确定性、无 RNG、
    每个通道混到整个排序域，避免 bodyId 局部聚类霸占单通道）。"""
    order = np.argsort(cell_ids, kind="stable")
    feat = np.empty(len(cell_ids), dtype=np.int64)
    feat[order] = np.arange(len(cell_ids)) % n_feat
    return feat


def build_real_mb(n_feat: int, dest: str | None = None) -> dict:
    """从连接组原始数据构建子图并写缓存。需要 pandas/pyarrow（仅构建时）。"""
    _, neurons = C.load_nodes()
    pre, post, w = C.load_edges()
    types = C.type_array(neurons)
    node = neurons["node_index"].to_numpy(dtype=np.uint64)
    body = neurons["bodyId"].to_numpy(dtype=np.uint64)

    kc_all = node[np.char.find(types, "KC") >= 0]
    mb07 = node[types == "MBON07"]
    mb11 = node[types == "MBON11"]

    to07 = np.isin(post, mb07) & np.isin(pre, kc_all)
    to11 = np.isin(post, mb11) & np.isin(pre, kc_all)
    substrate = np.union1d(np.unique(pre[to07]), np.unique(pre[to11]))

    # 外源喂入细胞：基底 KC 的所有上游中，排除 KC/APL（复发抑制）/DAN（调制）
    in_sub = np.isin(post, substrate)
    feeders = np.unique(pre[in_sub])
    ftypes = types[np.searchsorted(node, feeders)]
    keep = ((np.char.find(ftypes, "KC") < 0)
            & (np.char.find(ftypes, "APL") < 0)
            & (np.char.find(ftypes, "PAM") < 0)
            & (np.char.find(ftypes, "PPL") < 0)
            & (ftypes != "nan") & (ftypes != ""))
    ext = feeders[keep]
    ext = ext[np.argsort(body[np.searchsorted(node, ext)], kind="stable")]

    feat_of = _assign_features(ext, n_feat)

    # M[k, j] = 该 KC 收到来自通道 j 细胞的接触数合计（行 max 归一）
    ext_sub = in_sub & np.isin(pre, ext)
    ei = np.searchsorted(ext, pre[ext_sub])          # ext 已排序 -> searchsorted 即下标
    kj = np.searchsorted(substrate, post[ext_sub])   # substrate 已排序
    M = np.zeros((len(substrate), n_feat), dtype=np.float32)
    np.add.at(M, (kj, feat_of[ei]), w[ext_sub].astype(np.float32))
    rowmax = M.max(axis=1, keepdims=True)
    rowmax[rowmax == 0] = 1.0
    M = M / rowmax

    # KC -> MBON 接触权重（按目标组 max 归一；无边的 KC 为 0）
    c07 = np.zeros(len(substrate)); c11 = np.zeros(len(substrate))
    np.add.at(c07, np.searchsorted(substrate, pre[to07]), w[to07].astype(np.float64))
    np.add.at(c11, np.searchsorted(substrate, pre[to11]), w[to11].astype(np.float64))
    w07 = c07 / (c07.max() or 1.0)
    w11 = c11 / (c11.max() or 1.0)

    # ---- P1: 边级可塑基底 + 真实 DAN 增益矩阵（Huang/Luo 2024 规则用）-----
    # 边 = (KC, 目标组) 的接触数合并；学习规则按边工作
    edge_kc = np.concatenate([np.searchsorted(substrate, pre[to07]),
                              np.searchsorted(substrate, pre[to11])])
    edge_group = np.concatenate([np.zeros(int(to07.sum()), dtype=np.int8),
                                 np.ones(int(to11.sum()), dtype=np.int8)])
    edge_contacts = np.concatenate([w[to07], w[to11]]).astype(np.float64)
    dan_ids, dan_names = [], []
    for pat in ("PAM11", "PPL101"):
        sel = types == pat
        dan_ids.append(node[sel]); dan_names += [pat] * int(sel.sum())
    dan = np.concatenate(dan_ids).astype(np.uint64)
    n_dan = len(dan)
    # gain[d, e] = DAN d 落在边 e 目标小室的接触份额（按 DAN 行归一）。
    # 声明性建模选择，同 StonkFly（"anatomy-derived DAN-to-MBON contact
    # fractions distribute modulation within each compartment"）
    gain = np.zeros((n_dan, len(edge_kc)), dtype=np.float64)
    post07 = np.isin(post, mb07)
    post11 = np.isin(post, mb11)
    for di in range(n_dan):
        pre_d = np.isin(pre, dan[di:di + 1])
        c0 = float(w[post07 & pre_d].sum())
        c1 = float(w[post11 & pre_d].sum())
        tot = c0 + c1
        if tot > 0:
            gain[di, edge_group == 0] = c0 / tot
            gain[di, edge_group == 1] = c1 / tot

    out = {
        "kc_body": body[np.searchsorted(node, substrate)].astype(np.uint64),
        "M": M.astype(np.float32),
        "w07": w07, "w11": w11,
        "edge_kc": edge_kc.astype(np.int32),
        "edge_group": edge_group,
        "edge_contacts": edge_contacts,
        "gain": gain,
        "dan_names": np.array(dan_names),
        "n_ext": np.array([len(ext)]),
        "n_edges_ext": np.array([int(ext_sub.sum())]),
        "contacts_ext": np.array([int(w[ext_sub].sum())]),
        "n_edges_07": np.array([int(to07.sum())]),
        "n_edges_11": np.array([int(to11.sum())]),
        "feat_of_first": feat_of[:1],           # 冒烟：指派确定性的指纹
    }
    path = _cache_path(n_feat) if dest is None else dest
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(path, **out)
    return out


def load_real_mb(n_feat: int, dest: str | None = None) -> dict:
    """FlyBrain 运行路径：只读缓存；没有就现场构建一次（之后走缓存）。"""
    path = _cache_path(n_feat) if dest is None else dest
    if not os.path.exists(path):
        return build_real_mb(n_feat, dest=dest)
    z = np.load(path, allow_pickle=False)
    return {k: z[k] for k in z.files}
