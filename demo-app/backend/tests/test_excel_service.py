import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook, load_workbook

from app.excel_service import (
    create_export,
    normalize_code_fields,
    parse_history_workbook,
    parse_quote_workbook,
)


WORKSPACE = Path(__file__).resolve().parents[3]

# 真实询价/价目本体积较大，不随交付包分发。按以下顺序查找，找到即用，
# 都找不到则跳过相关用例，避免整套测试因缺夹具而报错。
FIXTURE_DIRS = [
    Path(os.getenv("QUOTE_TEST_DATA", "")),
    WORKSPACE / "测试数据",
    Path(r"E:\Browser Download\测试数据"),
    Path(r"E:\Browser Download\数据库文件"),
]


def fixture(name: str) -> str:
    """返回真实测试文件的绝对路径；不存在时跳过当前用例。"""
    for directory in FIXTURE_DIRS:
        if not str(directory):
            continue
        candidate = directory / name
        if candidate.exists():
            return str(candidate)
    pytest.skip(f"fixture 不存在: {name}（可用 QUOTE_TEST_DATA 指定目录）")


def test_real_procurement_templates_keep_expected_product_rows():
    # 14包 由 2427 修正为 2428：凯迪 sheet 第 1805 行「老师端探究设备」
    # （数量 52 座、规格列为空）此前被误当作空行丢弃，现已正常保留。
    fixtures = [("海南发改委14包.xlsx", 2428), ("海南发改委15包.xlsx", 1529)]
    for name, expected in fixtures:
        lines = parse_quote_workbook(fixture(name))
        assert len(lines) == expected


def test_formula_billing_quantity_uses_cached_excel_value():
    lines = parse_quote_workbook(fixture("海南发改委14包.xlsx"))
    calculator = next(line for line in lines if line["source_row"] == 7)

    assert calculator["quantity"] == 18
    assert calculator["pricing_quantity"] == 36


def test_plain_number_column_6_is_not_mistaken_for_quantity():
    """普教清单 column 6 is 含税单价 (a plain number); quantity must come from the 数量 column."""
    lines = parse_quote_workbook(fixture("普教清单.xlsx"))
    calculator = next(line for line in lines if line["name"] == "计算器")

    assert calculator["quantity"] == 13
    assert calculator["pricing_quantity"] == 13


def test_fuzzy_name_header_parses_real_quote_file():
    """高中理化生报价 uses 采购品目 / 参数当询价清单（含父级配置行需跳过）。"""
    lines = parse_quote_workbook(fixture("高中理化生报价.xlsx"))

    assert lines
    first = lines[0]
    assert first["name"] == "小推车"
    assert first["spec"]
    assert first["unit"] == "个"
    assert first["source_row"] == 3


def save_workbook(tmp_path, rows) -> str:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "询价单"
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    path = tmp_path / "quote.xlsx"
    path.write_bytes(stream.getvalue())
    return str(path)


def test_fuzzy_name_header_minimal_workbook(tmp_path):
    path = save_workbook(tmp_path, [
        ["序号", "器材名称", "规格、品名、教学性能要求", "单位", "数量", "不含税单价"],
        [1, "工作服", "材质：涤卡，身长≥120cm", "件", 5, None],
        [2, "绝缘手套", "橡胶材质或涤纶针织", "双", 5, None],
    ])
    lines = parse_quote_workbook(path)

    assert [line["name"] for line in lines] == ["工作服", "绝缘手套"]
    assert all(line["spec"] for line in lines)
    assert lines[0]["quantity"] == 5
    assert lines[0]["pricing_quantity"] == 5


def test_exact_aliases_win_over_fuzzy_name_matching(tmp_path):
    """制造商名称/规格型号 must not be swallowed by the fuzzy name/spec fallback."""
    path = save_workbook(tmp_path, [
        ["序号", "产品名称", "参数", "制造商名称", "规格型号", "单位", "数量"],
        [1, "电子天平", "100g，0.001g", "某某仪器厂", "XY-100", "台", 2],
    ])
    lines = parse_quote_workbook(path)

    assert lines[0]["name"] == "电子天平"
    assert lines[0]["manufacturer"] == "某某仪器厂"
    assert lines[0]["model"] == "XY-100"


def test_plain_price_column_does_not_override_quantity_minimal(tmp_path):
    path = save_workbook(tmp_path, [
        ["序号", "产品名称", "参数", "数量", "单位", "含税单价", "金额"],
        [1, "计算器", "10+2位数", 13, "个", 15.4, 200.2],
    ])
    lines = parse_quote_workbook(path)

    assert lines[0]["quantity"] == 13
    assert lines[0]["pricing_quantity"] == 13


def _stub_option(**kwargs):
    base = dict(
        selected=True,
        rank=1,
        score=50.0,
        final_price=10.0,
        warnings=[],
        history_quote=None,
        record_brand="",
        record_manufacturer="",
        record_model="",
        record_product_code="",
        record_spec="",
        record_unit="",
        record_source_file="",
        record_source_sheet="",
        record_source_row=0,
        manual_brand="",
        manual_manufacturer="",
        manual_model="",
        manual_spec="",
        manual_unit="",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _stub_line(**kwargs):
    base = dict(
        sheet_name="Sheet1",
        source_row=3,
        name="x",
        spec="",
        unit="只",
        quantity=1.0,
        pricing_quantity=1.0,
        status="review",
        recommended_score=0.0,
        warnings=[],
        confirmed=False,
        options=[],
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_export_header_uses_source_header_row_not_first_matched_row(tmp_path):
    """导出的报价列表头必须落在源表真正的表头行（此处第 2 行）。

    历史实现取 ``min(source_row) - 1``：源表开头若有未被解析成数据行的行，
    表头就会压到某条数据行上（表头错位、该行原有内容被覆盖）。
    """
    src = tmp_path / "quote.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(["采购物理器材清单(2026.09)"])                 # 第 1 行：标题
    sheet.append(["序号", "器材名称", "规格参数", "单位", "数量"])  # 第 2 行：表头
    sheet.append([1, "机械停表", None, "只", 6])                # 第 3 行：数据
    sheet.append([2, "液体内部压强演示器", "J2113型", "套", 6])   # 第 4 行：数据
    workbook.save(src)

    job = SimpleNamespace(
        tax_rate=0.10,
        source_file_path=str(src),
        requested_option_count=1,
        lines=[
            _stub_line(source_row=3, name="机械停表", options=[_stub_option()]),
            _stub_line(source_row=4, name="液体内部压强演示器", spec="J2113型", unit="套",
                       options=[_stub_option()]),
        ],
    )
    output = tmp_path / "out.xlsx"
    create_export(job, "internal", str(output))

    result = load_workbook(output)
    exported = result["Sheet1"]
    assert exported.cell(2, 1).value == "序号"        # 原表头完好，未被数据行顶替
    assert exported.cell(2, 6).value == "报价1单价"    # 新增报价列同样在第 2 行
    assert exported.cell(3, 1).value == 1             # 第 3 行仍是数据行
    assert exported.cell(3, 6).value == 10.0
    assert exported.cell(4, 1).value == 2
    result.close()


def test_spec_less_product_row_with_quantity_is_kept(tmp_path):
    """只有名称 + 数量、规格列为空的询价行必须保留。

    采购清单常整行不写规格（如（启明）2026秋采购器材），若按"有规格才算出产品行"
    过滤，这些商品会静默丢失、导出后整条不报价；同时它们还导致导出的表头行
    定位错误。真正该跳过的是"既无规格/编码/厂商、又无数量"的空行。
    """
    path = save_workbook(tmp_path, [
        ["序号", "器材名称", "规格参数", "单位", "数量"],
        [1, "机械停表", None, "只", 6],
        [2, "演示温度计", None, "只", 2],
        [3, "液体内部压强演示器", "J2113型", "套", 6],
        [4, "以下空白", None, None, None],  # 名称有内容但无数量 → 仍按空行跳过
    ])
    lines = parse_quote_workbook(path)

    assert [line["name"] for line in lines] == ["机械停表", "演示温度计", "液体内部压强演示器"]
    assert lines[0]["spec"] == ""
    assert lines[0]["quantity"] == 6
    assert lines[1]["quantity"] == 2


def test_five_digit_model_is_moved_to_product_code():
    record = normalize_code_fields(
        {"name": "电子起电机", "spec": "放电距离5mm～35mm", "model": "04013",
         "product_code": "", "unit": "台", "price": 380}
    )
    assert record["product_code"] == "04013"
    assert record["model"] == ""

    keep = normalize_code_fields(
        {"name": "小推车", "spec": "600*450*850", "model": "2020-1",
         "product_code": "", "unit": "个", "price": 270}
    )
    assert keep["model"] == "2020-1"
    assert keep["product_code"] == ""


def test_line_side_code_extraction_ignores_quantities(tmp_path):
    """5-digit JY codes in spec become product_code; rpm/lux numbers do not."""
    path = save_workbook(tmp_path, [
        ["序号", "器材名称", "主要技术参数", "单位", "数量"],
        [1, "电子起电机", "编码04013，放电距离5mm～35mm", "台", 2],
        [2, "电动离心机", "转速≥16000r/min，照度12000lx", "台", 1],
    ])
    lines = parse_quote_workbook(path)
    by_name = {line["name"]: line for line in lines}
    assert by_name["电子起电机"]["product_code"] == "04013"
    assert by_name["电动离心机"]["product_code"] == ""


def test_tax_inclusive_price_header_normalized_to_base(tmp_path):
    """“含税单价”表头的价格应折回税前基准，避免导出双重计税。"""
    from openpyxl import Workbook
    path = tmp_path / "tax.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "清单"
    ws.append(["序号", "产品名称", "规格", "单位", "含税单价", "金额"])
    ws.append([1, "一字螺丝刀", "一字", "把", 4.4, 4.4])
    ws.append([2, "注射器", "100mL", "支", 8.8, 8.8])
    wb.save(path)
    records = parse_history_workbook(str(path), "tax.xlsx")
    prices = {r["name"]: r["price"] for r in records}
    assert prices["一字螺丝刀"] == 4.0
    assert prices["注射器"] == 8.0


def test_csv_quote_and_history_parsing(tmp_path):
    """CSV（Excel 另存为，UTF-8 或 GBK 编码）应能按同一套表头规则解析。"""
    quote_csv = tmp_path / "inquiry.csv"
    quote_csv.write_bytes(
        "序号,器材名称,规格参数,单位,数量\n1,电子天平,100g,台,2\n".encode("utf-8-sig")
    )
    lines = parse_quote_workbook(str(quote_csv))
    assert len(lines) == 1
    assert lines[0]["name"] == "电子天平"
    assert lines[0]["quantity"] == 2

    history_csv = tmp_path / "history.csv"
    history_csv.write_bytes(
        "器材名称,规格参数,单位,单价,品牌,厂家\n托盘,300mm,个,5.5,赛特尔,宁波赛特尔教学仪器有限公司\n".encode("gbk")
    )
    records = parse_history_workbook(str(history_csv), "history.csv")
    assert len(records) == 1
    assert records[0]["price"] == 5.5
    assert records[0]["manufacturer"] == "宁波赛特尔教学仪器有限公司"


def save_history_workbook(tmp_path, rows) -> str:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "价目"
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    path = tmp_path / "history.xlsx"
    path.write_bytes(stream.getvalue())
    return str(path)


def test_wide_vendor_groups_import_as_separate_records(tmp_path):
    """宽表价目本（品牌1/厂家1/型号1、品牌2/…）一行拆成多厂商记录。"""
    path = save_history_workbook(tmp_path, [
        ["序号", "器材名称", "规格参数", "单位", "数量", "单价",
         "品牌1", "厂家1", "型号1", "品牌2", "厂家2", "型号2"],
        [1, "工作服", "涤卡材质", "件", 50, 35,
         "赛特尔", "宁波赛特尔教学仪器有限公司", "30802000110",
         "瑞仕达", "宁波瑞仕达教学仪器有限公司", "30802000110"],
        [2, "乳胶手套", "耐酸碱", "双", 50, 9,
         "赛特尔", "宁波赛特尔教学仪器有限公司", "30802000503",
         "", "", ""],
    ])
    records = parse_history_workbook(path, "wide.xlsx")
    assert len(records) == 3  # 第 2 行第 2 厂商组为空，只产出 1 条
    first, second = records[0], records[1]
    assert first["brand"] == "赛特尔" and first["manufacturer"].startswith("宁波赛特尔")
    assert second["brand"] == "瑞仕达" and second["manufacturer"].startswith("宁波瑞仕达")
    assert first["price"] == second["price"] == 35
    assert first["name"] == second["name"] == "工作服"
    # 5 位以外的编码保留在型号列（normalize_code_fields 只归位 5 位 JY 编码）
    assert first["model"] == "30802000110"
    assert records[2]["name"] == "乳胶手套" and records[2]["brand"] == "赛特尔"


def test_unnumbered_vendor_columns_stay_single_record(tmp_path):
    """单厂商表（无编号列组）维持原解析路径，一行一记录。"""
    path = save_history_workbook(tmp_path, [
        ["序号", "器材名称", "规格参数", "单位", "单价", "品牌", "厂家", "型号"],
        [1, "工作服", "涤卡材质", "件", 35, "赛特尔", "宁波赛特尔教学仪器有限公司", "30802000110"],
    ])
    records = parse_history_workbook(path, "single.xlsx")
    assert len(records) == 1
    assert records[0]["manufacturer"].startswith("宁波赛特尔")
    assert records[0]["model"] == "30802000110"


def test_real_xjhy_wide_workbook_imports_all_vendors():
    """真实 XJHY 宽表：全厂商入库且首现顺序 赛特尔→瑞仕达→德欧。"""
    records = parse_history_workbook(fixture("副本XJHY数据库.xlsx"), "副本XJHY数据库.xlsx")
    assert len(records) >= 14410  # 不低于离线长表转换导入的记录量
    seen: list[str] = []
    for record in records:
        identity = record["manufacturer"] or record["brand"]
        if identity and identity not in seen:
            seen.append(identity)
    assert seen[:3] == [
        "宁波赛特尔教学仪器有限公司",
        "宁波瑞仕达教学仪器有限公司",
        "余姚市德欧教学仪器厂",
    ]
