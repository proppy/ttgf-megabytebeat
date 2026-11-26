"""GF180MCU technology enablement on kfactory: PDK paths, layer map, and `ppolyf_u_1k_6p0` pcells."""

from __future__ import annotations

import os
from pathlib import Path

try:
    import kfactory as kf
    from kfactory import kdb
    from kfactory.layer import constant
except ImportError:  # pragma: no cover - layout pcells only needed for GDS generation
    kf = None  # type: ignore[assignment]
    kdb = None  # type: ignore[assignment]
    constant = None  # type: ignore[assignment]

PDK_ROOT = Path(os.environ.get("PDK_ROOT", "/home/exedev/workspace/pdk"))
GF180_DIR = PDK_ROOT / "gf180mcuD"
SC_DIR = GF180_DIR / "libs.ref/gf180mcu_fd_sc_mcu7t5v0"
SC_SPICE = SC_DIR / "spice/gf180mcu_fd_sc_mcu7t5v0.spice"
IO_SPICE = GF180_DIR / "libs.ref/gf180mcu_fd_io/spice/gf180mcu_fd_io.spice"
DRC_SCRIPT = GF180_DIR / "libs.tech/klayout/drc/run_drc.py"
DRC_ARGS = ("--variant=D",)
LVS_SCRIPT = GF180_DIR / "libs.tech/klayout/lvs/run_lvs.py"
LVS_ARGS = ("--variant=D", "--lvs_sub=VSS", "--top_lvl_pins")
NG_DESIGN = GF180_DIR / "libs.tech/ngspice/design.ngspice"
NG_LIB = GF180_DIR / "libs.tech/ngspice/sm141064.ngspice"
LYP_PATH = GF180_DIR / "libs.tech/klayout/tech/gf180mcu.lyp"

RES_MODEL = "ppolyf_u_1k_6p0"
RES_CELL = "horiz_hres_6p0"
HRES_GUARD_X = 1.20  # 0.45 dualgate->dummy + 0.40 dummy + 0.35 dummy->resistor (DV.8 / HRES.5)
HRES_HEAD_X = 0.81  # 0.61 poly head + 0.20 pplus overhang past res_mk end
HRES_GAP_X = 0.50  # minimum inter-finger pplus-to-pplus spacing
GRID = 0.005  # 5 nm manufacturing grid (um)
SITE_W = 0.56  # 7t5v0 placement/routing grid pitch (um)


def ngspice_pdk_files(pdk_root: Path | None = None) -> tuple[Path, Path, Path]:
    """Return `(design.ngspice, sm141064.ngspice, sc_spice)` under `pdk_root` (or `PDK_ROOT`)."""
    gf180_dir = Path(pdk_root) / "gf180mcuD" if pdk_root is not None else GF180_DIR
    return (
        gf180_dir / "libs.tech/ngspice/design.ngspice",
        gf180_dir / "libs.tech/ngspice/sm141064.ngspice",
        gf180_dir / "libs.ref/gf180mcu_fd_sc_mcu7t5v0/spice/gf180mcu_fd_sc_mcu7t5v0.spice",
    )


if kf is not None:

    class LAYER(kf.LayerEnum):
        """GF180MCU GDSII layer/datatype map used by passive analog layouts."""

        layout = constant(kf.kcl.layout)
        pr_bndry = (0, 0)
        poly2 = (30, 0)
        pplus = (31, 0)
        contact = (33, 0)
        metal1 = (34, 0)
        metal1_label = (34, 10)
        via1 = (35, 0)
        metal2 = (36, 0)
        metal2_label = (36, 10)
        sab = (49, 0)
        dualgate = (55, 0)
        resistor = (62, 0)
        res_mk = (110, 5)

    def _box(c: kf.DKCell, x0: float, y0: float, x1: float, y1: float, layer: int) -> None:
        c.shapes(layer).insert(kdb.DBox(round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)))

    @kf.cell
    def horiz_hres_6p0(w: float = 1.00, length: float = 10.00) -> kf.DKCell:
        """Bare horizontal GF180MCU `ppolyf_u_1k_6p0` unit finger with r0/r1 electrical Ports."""
        c = kf.DKCell()
        hl, hw = round(length / 2, 3), round(w / 2, 3)
        poly_end = round(hl + 0.61, 3)
        _box(c, -hl, -hw, hl, hw, LAYER.res_mk)
        _box(c, -poly_end, -hw, poly_end, hw, LAYER.poly2)
        _box(c, -(hl + 0.10), -(hw + 0.28), hl + 0.10, hw + 0.28, LAYER.sab)
        _box(c, -(poly_end + 0.20), -(hw + 0.20), -hl, hw + 0.20, LAYER.pplus)
        _box(c, hl, -(hw + 0.20), poly_end + 0.20, hw + 0.20, LAYER.pplus)
        for sign, pname, ori in ((-1, "r0", 180), (1, "r1", 0)):
            cx = round(sign * (hl + 0.43), 3)
            _box(c, cx - 0.11, -0.11, cx + 0.11, 0.11, LAYER.contact)
            _box(c, cx - 0.17, -0.30, cx + 0.17, 0.30, LAYER.metal1)
            c.create_port(
                name=pname,
                center=(cx, 0.0),
                width=0.34,
                orientation=ori,
                layer=LAYER.metal1,
                port_type="electrical",
            )
        return c

    def add_hres_row_guard(c: kf.DKCell, x0: float, x1: float, y: float, w: float = 1.00) -> None:
        """Draw GF180MCU dualgate, resistor, and flanking dummy poly2 enclosure for a row of fingers."""
        hw = round(w / 2, 3)
        res_x0 = round(x0 + HRES_GUARD_X, 3)
        res_x1 = round(x1 - HRES_GUARD_X, 3)
        _box(c, x0, y - (hw + 0.45), x1, y + (hw + 0.45), LAYER.dualgate)
        _box(c, res_x0, y - (hw + 0.40), res_x1, y + (hw + 0.40), LAYER.resistor)
        for dx0 in (res_x0 - 0.75, res_x1 + 0.35):
            _box(c, dx0, y - hw, dx0 + 0.40, y + hw, LAYER.poly2)
