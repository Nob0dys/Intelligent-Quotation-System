from app.matching import bigram_dice, compare_units, match_line, name_core, score_record


def record(**overrides):
    base = {
        "id": "h1",
        "name": "电子天平",
        "spec": "量程100g，精度0.001g，带防风罩",
        "product_code": "",
        "model": "FA1004",
        "brand": "测试品牌",
        "manufacturer": "测试仪器有限公司",
        "unit": "台",
        "price": 1200,
        "source_file": "历史.xlsx",
        "source_priority": 1,
    }
    base.update(overrides)
    return base


def line(**overrides):
    base = {
        "name": "电子天平",
        "spec": "量程100g，精度0.001g，带防风罩",
        "product_code": "",
        "model": "FA1004",
        "brand": "",
        "manufacturer": "",
        "unit": "台",
    }
    base.update(overrides)
    return base


def test_parameter_weight_selects_correct_same_name_candidate():
    right = record(id="right")
    wrong = record(id="wrong", spec="量程5000g，精度1g，教学用", model="JY-5")
    candidates = match_line(line(), [wrong, right])
    assert candidates[0].record["id"] == "right"
    assert candidates[0].component_scores["参数"] > candidates[1].component_scores["参数"]


def test_convertible_and_incompatible_units():
    converted = compare_units("克", "千克", 100)
    assert converted.status == "convertible"
    assert converted.normalized_price == 0.1
    blocked = compare_units("克", "瓶", 100)
    assert blocked.status == "incompatible"
    assert blocked.warning.startswith("BLOCK:")


def test_package_unit_converts_only_with_explicit_net_content():
    converted = compare_units("克", "瓶", 20, source_spec="每瓶净含量100克")
    assert converted.status == "convertible"
    assert converted.normalized_price == 0.2

    blocked = compare_units("克", "瓶", 20, source_spec="试剂一瓶")
    assert blocked.status == "incompatible"


def test_special_requirement_caps_confidence():
    candidate = score_record(
        line(),
        record(spec="塑料外壳，量程100g，精度0.001g"),
        [{"attribute_name": "材质", "operator": "contains", "value": "不锈钢", "required": True}],
    )
    assert candidate.confidence == "review"
    assert any(item.startswith("BLOCK:") for item in candidate.warnings)


def test_unrelated_product_is_not_reliable():
    candidate = score_record(line(), record(name="冰箱", spec="容积200L", model="BCD-200", unit="台"))
    assert candidate.confidence == "unreliable"


def test_digital_inquiry_downgrades_plain_variant():
    """数显/数字 inquiry must rank the digital variant above a plain one."""
    digital = record(id="digital", name="数显游标卡尺", spec="0～150mm，分辨力0.01mm，液晶显示", price=120)
    plain = record(id="plain", name="游标卡尺", spec="0～150mm，分度值0.02mm", price=30)
    candidates = match_line(
        {"name": "高中物理数显游标卡尺", "spec": "测量范围0mm～150mm，分辨力0.01mm", "unit": "把",
         "product_code": "", "model": "", "brand": "", "manufacturer": ""},
        [plain, digital],
    )
    assert candidates[0].record["id"] == "digital"
    assert any("数显" in item for item in candidates[1].warnings) or "规格变体" in "".join(candidates[1].warnings)


def test_capacity_mismatch_downgrades_wrong_capacity():
    """500g 托盘天平 must outrank a 200g candidate on the same line."""
    right = record(id="g500", name="托盘天平", spec="最大称量500g，分度值0.5g", price=55)
    short = record(id="g200", name="托盘天平", spec="最大称量200g，分度值0.2g", price=30)
    candidates = match_line(
        {"name": "高中物理托盘天平", "spec": "测量范围0g～500g，分度值0.5g", "unit": "台",
         "product_code": "", "model": "", "brand": "", "manufacturer": ""},
        [short, right],
    )
    assert candidates[0].record["id"] == "g500"
    assert any("量程不符" in item for item in candidates[1].warnings)


def test_pool_merges_exact_name_with_generic_name_candidates():
    """Exact-name match must not block generic-name price sources from joining."""
    exact = record(id="echo", name="高中物理数显游标卡尺", spec="测量范围0mm～150mm，分辨力0.01mm", price=30)
    generic = record(id="generic", name="数显游标卡尺", spec="150mm，0.01mm，液晶显示", price=120)
    candidates = match_line(
        {"name": "高中物理数显游标卡尺", "spec": "测量范围0mm～150mm，分辨力0.01mm", "unit": "把",
         "product_code": "", "model": "", "brand": "", "manufacturer": ""},
        [exact, generic],
    )
    ids = [c.record["id"] for c in candidates]
    assert "generic" in ids


def test_name_core_floor_separates_mismatches_from_real_products():
    """泛称/语义变体错配（仿真实验→静电实验箱、条形→蹄形强磁体）不满足自动带价门槛，
    而真实产品（电子起电机→电子起电机）满足，用于阻止自动带错价。"""
    from app.services import name_allows_autoselect
    assert name_allows_autoselect("高中物理仿真实验", "静电实验箱") is False
    assert name_allows_autoselect("高中生物水平电泳槽", "漏斗") is False
    assert name_allows_autoselect("高中物理条形强磁体", "蹄形强磁体") is False
    assert name_allows_autoselect("高中物理洛伦兹力演示器", "摩擦力演示器") is False
    assert name_allows_autoselect("高中生物解剖镊1", "普通手术剪") is False
    assert name_allows_autoselect("高中物理电子起电机", "电子起电机") is True
    assert name_allows_autoselect("高中物理多用电表1", "多用电表") is True
    assert name_allows_autoselect("高中物理数显游标卡尺", "数显游标卡尺") is True
    # 赛特尔价目本的前缀/材质变体（直尺↔钢直尺）放宽允许；其他来源仍拦截
    assert name_allows_autoselect("直尺", "钢直尺", "赛特尔25年.xls") is True
    assert name_allows_autoselect("直尺", "钢直尺", "普教清单.xlsx") is False
    # 一字之差/语义变体（槽码↔钩码）即使赛特尔来源也不允许；
    # 演示器↔实验器 按客户确认视为等价（摩擦力演示器=摩擦力实验器）
    assert name_allows_autoselect("金属槽码", "金属钩码", "赛特尔25年.xls") is False
    assert name_allows_autoselect("阿基米德原理演示器", "阿基米德原理实验器", "赛特尔25年.xls") is True
    assert name_allows_autoselect("演示螺旋测微器", "螺旋测微器", "赛特尔25年.xls") is False
    # 修饰词变体（演示/实验/内能/新型 差异）不允许自动带价
    assert name_allows_autoselect("演示斜面小车", "斜面小车", "赛特尔25年.xls") is False
    assert name_allows_autoselect("新型船闸模型", "船闸模型", "赛特尔25年.xls") is False
    assert name_allows_autoselect("机械能互变演示器", "机械能内能互变演示器", "赛特尔25年.xls") is False
    assert name_allows_autoselect("演示斜面小车", "演示斜面小车", "赛特尔25年.xls") is True
    assert name_allows_autoselect("斜面小车", "斜面小车", "赛特尔25年.xls") is True
    # 后缀超集的功能/型号前缀（图形计算器 ⊃ 计算器）是不同产品，拦截；
    # 前缀超集的组成说明（白卡纸带四方格、双面胶… ⊃ 白卡纸带四方格）是同一产品，放行
    assert name_allows_autoselect("计算器", "图形计算器", "赛特尔25年.xls") is False
    assert (
        name_allows_autoselect(
            "白卡纸(带四方格)", "白卡纸( 带四方格 )、双面胶、线绳、细沙等", "赛特尔25年.xls"
        )
        is True
    )


def test_exact_code_does_not_exclusively_lock_candidate_pool():
    """跨价目本编号体系冲突（预算清单 01013=计算器 vs 赛特尔 01013=图形计算器）
    时，同码记录不得独占候选池——精确同名候选必须参与评分。"""
    code_collision = record(
        id="gcalc",
        name="图形计算器",
        spec="具有常规计算、图象/表格、方程求解、简单程序编制等功能",
        product_code="01013",
        price=600,
    )
    exact = record(
        id="calc",
        name="计算器",
        spec="简易型。8位单行LCD显示、四则运算、开平方、独立储存器",
        product_code="01012",
        price=14,
        unit="个",
    )
    candidates = match_line(
        line(name="计算器", spec="简易型。", product_code="01013", unit="个", model=""),
        [code_collision, exact],
    )
    ids = [c.record["id"] for c in candidates]
    assert "calc" in ids


def test_spec_ranges_extracts_ranges():
    """P0: spec_ranges should extract ranges from various spec formats."""
    from app.matching import spec_ranges
    assert spec_ranges("Φ7～8mm") == {"mm": (7.0, 8.0)}
    assert spec_ranges("φ7mm～8mm") == {"mm": (7.0, 8.0)}
    r = spec_ranges("250mm×180mm×100mm")
    assert r["mm"] == (100.0, 250.0)
    assert r["dims"] == [100.0, 180.0, 250.0]
    assert spec_ranges("-30~50℃") == {"℃": (-30.0, 50.0)}
    assert spec_ranges("-50～40℃") == {"℃": (-50.0, 40.0)}
    # Single value now extracted as (value, value) range
    assert spec_ranges("150mm") == {"mm": (150.0, 150.0)}
    assert spec_ranges("量程100g，精度0.001g") == {"g": (100.0, 100.0)}


def test_spec_features_extracts_new_dimensions():
    """P1: _spec_features should extract shape, material, magnification."""
    from app.matching import _spec_features
    # Shape
    feat = _spec_features("干燥管 U型，φ15mm×150mm")
    assert feat["shape"] == "U型"
    feat = _spec_features("干燥管 单球，150mm")
    assert feat["shape"] == "单球"
    # Material
    feat = _spec_features("试管 高硼硅玻璃材质，试管外径Φ12mm")
    assert feat["material"] == "高硼硅"
    feat = _spec_features("玻璃管 透明钠钙玻璃材质；外径Φ7mm~Φ8mm")
    assert feat["material"] == "钠钙"
    # Magnification
    feat = _spec_features("学生显微镜 200×，单筒")
    assert feat["magnification"] == "200×"
    feat = _spec_features("显微镜 200×10")
    assert feat["magnification"] == "200×10"


def test_parameter_similarity_range_comparison():
    """P0: parameter_similarity should give bonus for overlapping ranges."""
    from app.matching import parameter_similarity
    # Same range, different format
    sim = parameter_similarity("Φ7～8mm", "φ7mm～8mm")
    assert sim >= 0.5  # Should be improved from 0.267
    # Different temperature ranges
    sim = parameter_similarity("-30~50℃", "-50～40℃")
    assert sim >= 0.3  # Should be improved from 0.267
    # Identical 3D dimensions
    sim = parameter_similarity("250mm×180mm×100mm", "250mm×180mm×100mm")
    assert sim >= 0.9


def test_equal_range_is_not_a_variant_mismatch():
    """同值量程不得误报“规格量程不符”（100mm vs 100mm），否则正确候选被踢出默认池。"""
    from app.matching import variant_penalty
    penalty, warnings = variant_penalty(
        {"name": "量筒", "spec": "100ml"},
        {"name": "量筒", "spec": "100ml，分度值1ml"},
    )
    assert not any("量程不符" in warning for warning in warnings)
    assert penalty == 0


def test_name_core_normalizes_price_book_suffix_and_terminology():
    """价目本导出的尾字母脏后缀与 磁体/磁铁 术语要归一，同名加分才能生效。"""
    assert name_core("光的传播、反射、折射实验器c") == name_core("光的传播、反射、折射实验器")
    assert name_core("LED光源a") == name_core("LED光源")
    assert name_core("蹄形磁铁") == name_core("蹄形磁体")


def test_jy_major_only_applies_to_five_digit_codes():
    """11 位新课标分类代码（30307216301）不得按旧表前两位误判成“光学”。"""
    from app.category import jy_major
    assert jy_major("02011") == "通用仪器"
    assert jy_major("30307216301") == ""
    assert jy_major("30307216301.0") == ""
    assert jy_major("S7261") == ""


def test_normalize_code_strips_export_dot_zero():
    from app.matching import normalize_code
    assert normalize_code("30307216301.0") == "30307216301"
    assert normalize_code("02045") == "02045"
    assert normalize_code("") == ""


def test_same_name_price_spread_is_not_penalized():
    """同名多规格（电阻箱 45/80）的价差是版本差异，价格一致性不得把高配版扣分。"""
    cheap = record(id="cheap", name="电阻箱", spec="", price=45, unit="个", source_file="赛特尔25年.xls")
    rich = record(id="rich", name="电阻箱", spec="六位99999.9Ω，0.1级", price=80, unit="个", source_file="赛特尔25年.xls")
    peer_plain = record(id="peer1", name="电阻箱", spec="四位9999Ω", price=45, unit="个", source_file="赛特尔25年.xls")
    peer_other = record(id="peer2", name="教学电阻箱", spec="9999.9Ω", price=65, unit="个", source_file="赛特尔25年.xls")
    candidates = match_line(
        {"name": "电阻箱", "spec": "/", "unit": "个", "product_code": "", "model": "",
         "brand": "", "manufacturer": ""},
        [cheap, rich, peer_plain, peer_other],
    )
    by_id = {candidate.record["id"]: candidate for candidate in candidates}
    assert by_id["cheap"].component_scores["价格一致性"] == 6.0
    assert by_id["rich"].component_scores["价格一致性"] == 6.0


def test_suffix_cleanup_lets_new_standard_item_win_exact_name():
    """“光的传播、反射、折射实验器c”清洗后应拿到精确同名加分，压过无规格旧版。"""
    old = record(
        id="old", name="光的传播、反射、折射实验器", spec="", price=22, unit="台",
        source_file="赛特尔25年.xls",
    )
    new = record(
        id="new", name="光的传播、反射、折射实验器c",
        spec="包括能显示光路的透明材料制成的半圆玻砖、角度板、两个条形玻砖、半导体激光光源等",
        price=58, unit="台", source_file="赛特尔25年.xls",
    )
    candidates = match_line(
        {"name": "光的传播、反射、折射实验器", "spec": "/", "unit": "台", "product_code": "",
         "model": "", "brand": "", "manufacturer": ""},
        [old, new],
    )
    assert candidates[0].record["id"] == "new"


def _saitel_record(record_id, name, spec, unit, price, code="", sheet=""):
    return record(
        id=record_id, name=name, spec=spec, unit=unit, price=price, product_code=code,
        source_sheet=sheet, source_file="赛特尔25年.xls", model="",
    )


def _plain_line(name, spec, unit, code=""):
    return {
        "name": name, "spec": spec, "unit": unit, "product_code": code,
        "model": "", "brand": "", "manufacturer": "",
    }


def test_spec_range_phi_prefix_on_both_numbers():
    """φ5～φ6mm / φ5mm～φ6mm 必须解析成同一范围，不得互报量程不符。"""
    from app.matching import spec_ranges, variant_penalty
    assert spec_ranges("φ5～φ6mm")["mm"] == (5.0, 6.0)
    assert spec_ranges("φ5mm～φ6mm")["mm"] == (5.0, 6.0)
    penalty, warnings = variant_penalty(
        {"name": "玻璃棒", "spec": "φ5～φ6mm，长≥300mm，高硼硅"},
        {"name": "玻璃棒", "spec": "φ5mm～φ6mm"},
    )
    assert not any("量程不符" in warning for warning in warnings)


def test_multi_value_compare_ignores_irrelevant_number():
    """手摇离心转台：140mm（支杆距离）不得与候选支杆直径 10mm 误报量程不符。"""
    from app.matching import variant_penalty
    penalty, warnings = variant_penalty(
        {"name": "手摇离心转台", "spec": "从动轮轴心与支杆中心距离≥140mm，轴孔上段≥Ф10mm"},
        {"name": "手摇离心转台", "spec": "支杆直径 10 mm，全长 140 mm"},
    )
    assert not any("量程不符" in warning for warning in warnings)


def test_dissecting_toolkit_count_discrimination():
    """解剖器 7件/4件 必须区分：不能两个规格同价。"""
    seven = _saitel_record("7件", "解剖器", "7件", "套", 22, "27001", "初中生物")
    four = _saitel_record("4件", "解剖器", "4件", "套", 14, "27002", "初中生物")
    line7 = _plain_line("解剖器", "1、规格：7件 2、由解剖器和定位包装袋组成，不锈钢", "套")
    line4 = _plain_line("解剖器", "1、规格：4件 2、由剪刀、镊子、解剖刀、解剖针组成", "套")
    assert match_line(line7, [seven, four])[0].record["id"] == "7件"
    assert match_line(line4, [seven, four])[0].record["id"] == "4件"


def test_heart_model_scale_discrimination():
    """心脏解剖模型 3倍自然大(50) vs 自然大(40) 必须区分。"""
    triple = _saitel_record("三倍", "心脏解剖模型", "3倍自然大", "件", 50, "33207", "初中生物")
    natural = _saitel_record("自然大", "心脏解剖模型", "自然大", "件", 40, "33208", "初中生物")
    line_natural = _plain_line("心脏解剖模型", "自然大，PVC制，从心尖部至主动脉根部长 85mm", "件")
    line_triple = _plain_line("心脏解剖模型", "1.规格：3 倍自然大；2.沿左右心耳上方剖开", "件")
    assert match_line(line_natural, [triple, natural])[0].record["id"] == "自然大"
    assert match_line(line_triple, [triple, natural])[0].record["id"] == "三倍"


def test_flask_bottom_shape_discrimination():
    """烧瓶 圆底/平底 是不同规格，圆底询价不得选平底记录。"""
    flat = _saitel_record("平底", "烧瓶", "平、长，250ml", "个", 9, "61037", "初中物理")
    round_b = _saitel_record("圆底", "烧瓶", "圆、长，250mL", "个", 9, "61033", "初中化学")
    line = _plain_line("烧瓶", "3.3 硼硅玻璃，细口圆底烧瓶，标称容量 250ml，全高 145mm", "个")
    assert match_line(line, [flat, round_b])[0].record["id"] == "圆底"


def test_grouped_molecular_model_prefers_group_variant():
    """32003 分子结构模型：学生分组用应选分组用(40)，而不是演示用(140)/初中用(55)。"""
    demo = _saitel_record("演示", "分子结构模型", "演示用", "套", 140, "32003", "高中化学")
    grouped = _saitel_record("分组", "分子结构模型", "分组用", "套", 40, "32003", "高中化学")
    junior = _saitel_record("初中", "分子结构模型", "初中用", "套", 55, "32003", "初中化学")
    line = _plain_line(
        "分子结构模型",
        "初中分组学生用，可组合初中分子结构模型：氢气、氧气、水分子、二氧化碳分子、甲烷等",
        "套", "32003",
    )
    assert match_line(line, [demo, grouped, junior])[0].record["id"] == "分组"


def test_extreme_price_outlier_same_name_is_demoted():
    """玻璃棒：同规格记录多数为 12 元，1.3 元异常低价不得当选。"""
    records = [
        _saitel_record("cheap", "玻璃棒", "φ5mm～φ6mm", "个", 1.3, "64054", "小学科学"),
        _saitel_record("kg1", "玻璃棒", "φ5～φ6mm", "千克", 12.0, "64054", "初中化学"),
        _saitel_record("kg2", "玻璃棒", "φ5mm～φ6mm", "千克", 12.0, "64053", "高中化学"),
        _saitel_record("kg3", "玻璃棒", "φ5mm～6mm", "千克", 12.0, "64053", "高中生物"),
        _saitel_record("kg4", "玻璃棒", "Φ 5 mm ～6 mm 粗细均匀", "kg", 12.0, "30605005302", "初中新课标化学"),
        _saitel_record("root", "玻璃棒", "Φ5mm~Φ6mm，两端烧结", "根", 2.0, "30605005302", "小学新课标科学"),
    ]
    line = _plain_line("玻璃棒", "1、规格：φ5～φ6mm 2、高硼硅 3、长≥300mm，两端做烧结处理", "个")
    candidates = match_line(line, records)
    assert candidates[0].record["id"] != "cheap"
    assert candidates[0].record["price"] == 12.0


def test_config_price_delta_pulley_stop_rule():
    """21032 滑轮组含可止动配置：老课标基础价 9 元 + 6 元 = 15 元。"""
    from app.services import config_price_delta
    delta, note = config_price_delta(
        {"name": "滑轮组", "spec": "1.学生用；2.每个滑轮组中应至少有一个可止动滑轮"},
        {"product_code": "21032", "name": "滑轮组", "price": 9.0},
    )
    assert delta == 6.0
    assert "止动" in note
    delta_plain, _ = config_price_delta(
        {"name": "滑轮组", "spec": "由双滑轮2只（串），单滑轮2只组成"},
        {"product_code": "21032", "name": "滑轮组", "price": 9.0},
    )
    assert delta_plain == 0.0


def test_inquiry_typo_centrifuge_stand_matches():
    """询价错字"手摇离心钻台"应归一为"手摇离心转台"。"""
    assert name_core("手摇离心钻台") == name_core("手摇离心转台")
