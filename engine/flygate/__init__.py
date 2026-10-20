"""FlyGate — a fruit-fly-brain (connectome-inspired) crypto trading system on Gate.io.

Architecture summary (per coin, one independent "fly brain"):

    Gate 1m candles ──► chart render (48x27 grid) ──► retina (R1-R6 luminance, R8 color)
        ──► lamina LMC center-surround contrast
        ──► medulla T4/T5 vertical motion detectors (Hassenstein-Reichardt EMD)
        ──► lobula-plate HS_up / HS_dn pooling cells
        ──► descending neurons DN_up / DN_dn  +  commitment DN (gated by mushroom body)
        ──► decision: BUY / SELL / HOLD

    Learning (mushroom body, online only — no offline training):
        equity delta per observation ──► reward DAN (PAM) / punishment DAN (PPL)
        KC→MBON synapses follow a dopamine-gated anti-Hebbian rule
        (Huang & Luo 2024 style, as adopted by StonkFly), gating the commitment DN.

This is a cell-type-level reduction of the real Drosophila circuit (FlyWire/MaleCNS
cell classes and pathway logic are real; neuron counts are reduced so it runs on a
laptop CPU). The Brain interface is designed so a full MaleCNS kernel can be
swapped in later.
"""

__version__ = "0.1.0"
