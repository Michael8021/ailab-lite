"""AILab 简易版（本机 · 单节点 k3s）架构图。风格沿用 AILab/architecture/diagrams/src/svglib.py。

生成：python3 -I d_lite_arch.py   →  ../svg/lite-01-architecture.svg
渲染：python3 -I render.py        →  ../png/lite-01-architecture.png
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from svglib import Canvas, tw  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "svg", "lite-01-architecture.svg")
ORANGE = "#c2410c"

c = Canvas(1500, 1100, "AILab 简易版架构", "本机 · 单节点 k3s · 与大集群同组件、同部署文件，只有 overlays/lite 的数值不同",
           legend=[("own", "自研服务"), ("oss", "开源组件"), ("data", "数据"), ("sto", "存储"),
                   ("sbx", "沙箱"), ("warn", "本机已有")])

def rb(x, y, w, label, sub=None):
    """分区标题右对齐，避免被自上而下的箭头穿过。"""
    c.text(x + w - 14, y + 24, label, 15, weight=700, color="#334155", anchor="end")
    if sub:
        c.text(x + w - 14 - tw(label, 15) - 12, y + 24, sub, 12, color="#64748b", anchor="end")


# ---------------------------------------------------------------- 顶部：用户与外部
c.box(70, 66, 250, 62, "浏览器 / Portal 用户", ["SSE 流式 · 预签名直传 S3"], style="usr")
c.box(380, 66, 320, 62, "DeepSeek 官方 API", [("api.deepseek.com · 数据出域，只用测试数据", "#dc2626")],
      style="usr", dash="6 4")

# ---------------------------------------------------------------- 宿主机 / k3s 外框
c.rect(20, 150, 1460, 784, fill="#fcfcfd", stroke="#64748b", sw=1.4, rx=12, dash="8 5")
c.text(500, 172, "宿主机：VMware 虚拟机 · 2 vCPU · 7.7 GB · x86_64 · 无 KVM（所以沙箱用 gVisor，不用 Kata）",
       13, weight=700, color="#334155")
c.rect(36, 190, 1134, 726, fill="#f8fafc", stroke="#0891b2", sw=1.6, rx=10)
c.text(500, 212, "k3s v1.34.12 · 单节点（大集群：kubeadm 1.34）· 已关 traefik / servicelb / metrics-server",
       13, weight=700, color="#0e7490")

# ---------------------------------------------------------------- 行 A：接入与模型面
c.band(52, 232, 1102, 140, None, "gray")
rb(52, 232, 1102, "接入与模型面", "higress-system · gpustack · identity")
c.box(70, 274, 380, 88, "Higress 2.1.9", ["入口路由 + 模型网关（本机合并为一套）", "插件：Key 鉴权 · 用量 · 按人限流",
                                         "路由由 GPUStack 以 incluster 模式写入"], style="oss", badge="T3")
c.box(520, 274, 300, 88, "GPUStack Server 2.3", ["只有 Server，没有 Worker", "DeepSeek 配成“模型提供方”",
                                                 "别名：ailab-agent-pro / agent / chat"], style="oss", badge="T3")
c.box(860, 274, 270, 88, "Keycloak 26.x（Operator）", ["本地测试用户，不接 LDAP", "Platform API 以 OIDC 对接（BFF）"],
      style="oss", badge="后续")

# ---------------------------------------------------------------- 行 B：平台（自研）
c.band(52, 408, 1102, 132, None, "gray")
rb(52, 408, 1102, "平台服务 · platform", "自研，各 1 副本（同一份镜像，大集群只改副本数）")
rowB = [("Portal", ["Next.js", "Chat / Work / 资料库"], 64, "后续"),
        ("Platform API", ["身份 · 权限 · 配额", "资料库 · 账本（唯一写入方）"], 284, "后续"),
        ("Agent Service", ["deepagents 循环（在沙箱外）", "只有它调用模型"], 504, "I1"),
        ("Sandbox Broker", ["沙箱准入 · 租约 · 回收", "只搬运文件字节"], 724, "I2"),
        ("文件处理 Worker", ["解析 · 预览 · 病毒扫描", "无 OCR 模型，扫描件先跳过"], 944, "后续")]
for title, lines, x, badge in rowB:
    c.box(x, 448, 196, 84, title, lines, style="own", title_size=14.5, line_size=12, badge=badge)

# ---------------------------------------------------------------- 行 C：数据 / OpenSandbox
c.band(52, 578, 508, 130, "数据面 · data", "gray", sub="单实例")
c.box(64, 618, 150, 80, "PostgreSQL", ["CNPG · instances=1", "业务库 · checkpoint"], style="data", badge="T4", title_size=14)
c.box(228, 618, 150, 80, "Valkey", ["限流计数 · run 队列", "事件流（可丢）"], style="data", badge="T3", title_size=14)
c.box(392, 618, 156, 80, "SeaweedFS S3", ["代替 Pacific S3", "资料库 · 预签名"], style="sto", badge="T4", title_size=14)

c.band(578, 578, 576, 130, None, "gray")
c.text(665, 602, "沙箱控制 · opensandbox-system", 15, weight=700, color="#334155")
c.box(590, 618, 170, 80, "ingress-gateway", ["所有 execd 流量入口", "校验单沙箱令牌"], style="oss", badge="T2", title_size=14)
c.box(776, 618, 170, 80, "Controller", ["预热池 0～1 个", "到期回收（兜底）"], style="oss", badge="T2", title_size=14)
c.box(962, 618, 170, 80, "Server", ["生命周期 API", "运行时 = gvisor"], style="oss", badge="T2", title_size=14)

# ---------------------------------------------------------------- 行 D：沙箱
c.band(52, 752, 1102, 148, "沙箱 · namespace sandbox · runtimeClass = gvisor（大集群：kata-qemu）", "sbx")
c.box(70, 792, 510, 96, None, [
    ("NetworkPolicy：默认拒绝全部出入", "#166534"),
    ("（gVisor 下没有出网 sidecar，只靠这一层）", "#166534"),
    ("ResourceQuota：pods = 1（同时只有 1 个沙箱）", "#166534"),
    ("无密钥 · 无 SA Token · 非 root · 最长存活 4 小时", "#166534"),
], style="ok", align="left", line_size=12.5, badge="T0")
c.box(600, 792, 530, 96, "沙箱 Pod（最多 1 个）", [
    "sbx-office:lite · Python · LibreOffice · 中文字体 · Skills",
    "1 CPU · 1.5 GiB · /workspace/inputs · outputs（临时）",
    "镜像预导入 k3s，名字与大集群一致（不靠拉取）",
], style="sbx", badge="T1", title_size=14.5, line_size=12)

# ---------------------------------------------------------------- 右侧：本机已有服务
c.box(1190, 190, 276, 170, "本机已有服务（不得影响）", [
    "本机原有的业务服务",
    "  （只读探测，不改配置）",
    "反向代理 :80 · redis :6379",
    "其他常驻进程",
], style="warn", dash="6 4", title_size=14, line_size=12.5)
c.box(1190, 380, 276, 112, "T0 实测", [("docker 自己的 iptables 链无变化", "#166534"),
                                       ("k3s 仅在 FORWARD 前插 pod 网段规则", "#166534"),
                                       ("宿主机探针前后均正常", "#166534"),
                                       ("原有服务的端口不变", "#166534")], style="ok", title_size=13.5, line_size=12)
c.box(1190, 512, 276, 112, "资源", [("全套估算 6.5～7 GB > 7.7 GB", "#b91c1c"),
                                    ("装不下 → 一次只起一组", "#b91c1c"),
                                    ("（T 标签 = 测试分组）", "#475569"),
                                    ("k3s 本身约 +0.5 GB（实测）", "#475569")], style="plain", title_size=13.5, line_size=12)

# ---------------------------------------------------------------- 箭头
# ① 浏览器 → Higress（入口）
c.arrow([(195, 128), (195, 274)], color="blue", label="HTTPS / SSE", lpos=(203, 143), lanchor="start")
c.num(195, 240, 1)
# ④ Higress → DeepSeek（出网）
c.arrow([(440, 274), (440, 128)], color="orange", label="出网 · provider 路由", lpos=(448, 143), lanchor="start")
c.num(440, 240, 4, color="#ea580c")
# GPUStack → Higress（写路由 / 插件）
c.arrow([(520, 318), (450, 318)], color="teal", dash=True, label="写路由", lpos=(485, 310))
# ② Higress → Portal / Platform API（入口路由）
c.arrow([(130, 362), (130, 448)], color="blue")
c.arrow([(330, 362), (330, 448)], color="blue", label="入口路由", lpos=(338, 410), lanchor="start")
c.num(130, 400, 2)
# ③ Agent → Higress（LLM 调用）
c.arrow([(602, 448), (602, 398), (420, 398), (420, 362)], color="teal",
        label="服务 Key + x-user-id · LLM 调用", lpos=(430, 391), lanchor="start")
c.num(602, 424, 3, color="#0e7490")
# Platform API ⇄ Agent Service
c.arrow([(480, 490), (504, 490)], color="purple", head="both", sw=1.8)
# ⑦ Platform API → 数据面
c.arrow([(300, 532), (300, 578)], color="purple", label="SQL · 预签名 · 队列", lpos=(320, 560), lanchor="start")
c.num(300, 553, 7, color="#7c3aed")
# ⑤ Agent → ingress-gateway → 沙箱（exec）
c.arrow([(630, 532), (630, 618)], color="blue", label="exec · 读写文件", lpos=(642, 566), lanchor="start")
c.num(630, 553, 5)
c.arrow([(675, 698), (675, 792)], color="blue", label="单沙箱令牌", lpos=(683, 748), lanchor="start")
# ⑥ Broker → OpenSandbox Server（acquire / release）
c.arrow([(880, 532), (880, 566), (1047, 566), (1047, 618)], color="gray", label="acquire / release", lpos=(965, 560))
c.num(880, 550, 6, color="#475569")
# Controller → 沙箱 Pod（创建 / 回收）
c.arrow([(861, 698), (861, 792)], color="gray", dash=True, label="创建 / 回收 Pod", lpos=(869, 748), lanchor="start")

# ---------------------------------------------------------------- 底部说明
c.rect(20, 948, 720, 134, fill="#fefce8", stroke="#ca8a04", sw=1, rx=10)
c.text(38, 972, "与大集群的差异（迁移时只需重新验证这几项，其余组件和部署文件一致）", 13, weight=700, color="#713f12")
c.bullets(38, 996, [
    "K8s：kubeadm + Calico  →  k3s + flannel（应用无感）",
    "沙箱隔离：Kata（QEMU） → gVisor；出网控制只剩 NetworkPolicy",
    "模型：本地 vLLM（GPUStack Worker） → DeepSeek 官方 API（改 GPUStack 路由目标即可切回）",
    "存储与副本：Pacific S3 → SeaweedFS；PG / Valkey 单实例；身份用本地测试用户",
    "CPU 架构：arm64 → 本机 x86_64，自研镜像和沙箱镜像必须双架构构建",
], size=12, gap=19)

c.rect(760, 948, 720, 134, fill="#fefce8", stroke="#ca8a04", sw=1, rx=10)
c.text(778, 972, "一次“帮我做个 PPT”的主路径（数字对应图中箭头）", 13, weight=700, color="#713f12")
c.bullets(778, 996, [
    "① 浏览器经 Higress 入口 → ② Platform API 校验身份、建 Run（run_id 进 Valkey 队列）",
    "③ Agent Service 经 Higress 调模型 → ④ Higress 经 GPUStack 路由转发到 DeepSeek",
    "⑥ Broker 向 OpenSandbox 领沙箱 → ⑤ Agent 经 ingress-gateway 在沙箱里生成 pptx",
    "⑦ 产出由 Broker 经两阶段提交写入 S3，Platform API 登记版本、推事件给浏览器",
    "分组测试：T0 基座 ✅ · T1 沙箱镜像 · T2 OpenSandbox · T3 模型面 · T4 数据面 · I1/I2 协同",
], size=12, gap=19)

c.save(OUT)
print("saved", os.path.normpath(OUT))
