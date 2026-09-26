import os
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.excel_service import normalize_code_fields, parse_history_workbook, parse_quote_workbook


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
    fixtures = [("海南发改委14包.xlsx", 2427), ("海南发改委15包.xlsx", 1529)]
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
