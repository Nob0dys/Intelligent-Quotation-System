from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import select, update

from app.database import SessionLocal
from app.main import app
from app.matching import normalize_text
from app.models import HistoryQuote, QuoteOption


def workbook_bytes(rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "询价单"
    sheet.append(["序号", "产品名称", "参数", "型号", "单位", "数量"])
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def single_line_workbook() -> bytes:
    return workbook_bytes([[1, "电子天平", "100g，0.001g，带防风罩", "", "台", 2]])


def login(client: TestClient, username: str = "admin", password: str = "admin123"):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def create_job(client: TestClient, customer_id: int, content: bytes | None = None, option_count: int = 3) -> str:
    response = client.post(
        "/api/quote-jobs",
        data={"customer_id": customer_id, "requested_option_count": option_count},
        files={"file": ("询价.xlsx", content or single_line_workbook(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def ordinary_customer_id(client: TestClient) -> int:
    customers = client.get("/api/customers").json()
    return next(item for item in customers if item["customer_type"] == "ordinary")["id"]


def test_export_without_confirmation_allowed():
    """未确认任何行也能导出（客户版未确认行留空），不再 409 阻断。"""
    with TestClient(app) as client:
        login(client)
        job_id = create_job(client, ordinary_customer_id(client))
        export = client.post(f"/api/quote-jobs/{job_id}/export/customer")
        assert export.status_code == 200, export.text
        workbook = load_workbook(BytesIO(export.content))
        assert "询价单" in workbook.sheetnames


def test_auto_confirm_and_export_flow():
    """一键导出：自动带价行被确认并带价导出；无自动带价行保持未确认。"""
    with TestClient(app) as client:
        login(client)
        job_id = create_job(client, ordinary_customer_id(client))
        lines = client.get(f"/api/quote-jobs/{job_id}/lines").json()
        assert lines["total"] >= 1
        auto_priced = sum(1 for it in lines["items"] if (it.get("selected_option_count") or 0) > 0)
        response = client.post(f"/api/quote-jobs/{job_id}/auto-confirm-and-export/customer")
        assert response.status_code == 200, response.text
        job = client.get(f"/api/quote-jobs/{job_id}").json()
        assert job["confirmed_lines"] == auto_priced
        workbook = load_workbook(BytesIO(response.content))
        sheet = workbook["询价单"]
        # 至少一行有确认单价（自动带价行）
        priced = [sheet.cell(2, c).value for c in range(1, sheet.max_column + 1) if isinstance(sheet.cell(2, c).value, (int, float))]
        assert any(isinstance(v, (int, float)) for v in priced)


def test_lines_sort_score_asc():
    with TestClient(app) as client:
        login(client)
        content = workbook_bytes([
            [1, "电子天平", "100g，0.001g，带防风罩", "", "台", 2],
            [2, "不存在的仪器XYZ", "定制参数abc", "", "台", 1],
        ])
        job_id = create_job(client, ordinary_customer_id(client), content)
        default = client.get(f"/api/quote-jobs/{job_id}/lines").json()
        assert default["total"] == 2
        assert default["items"][0]["source_row"] == 2
        sorted_result = client.get(f"/api/quote-jobs/{job_id}/lines?sort=score_asc").json()
        scores = [item["recommended_score"] for item in sorted_result["items"]]
        assert scores == sorted(scores)
        assert sorted_result["items"][0]["recommended_score"] <= default["items"][0]["recommended_score"]


def test_export_multi_option_quantity_and_totals():
    with TestClient(app) as client:
        login(client)
        job_id = create_job(client, ordinary_customer_id(client), option_count=3)
        lines = client.get(f"/api/quote-jobs/{job_id}/lines").json()
        line = client.get(f"/api/quote-lines/{lines['items'][0]['id']}").json()
        # select up to 3 options with distinct manufacturers
        seen = set()
        chosen = []
        for option in line["options"]:
            key = normalize_text(option["record"]["manufacturer"] or option["record"]["brand"]) or option["id"]
            if key in seen:
                continue
            seen.add(key)
            chosen.append(option["id"])
            if len(chosen) == 3:
                break
        update_response = client.patch(
            f"/api/quote-lines/{line['id']}",
            json={"selected_option_ids": chosen, "final_prices": {}, "manual_note": ""},
        )
        assert update_response.status_code == 200, update_response.text
        confirm = client.post(f"/api/quote-lines/{line['id']}/confirm", json={"override_reason": "测试"})
        assert confirm.status_code == 200, confirm.text

        export = client.post(f"/api/quote-jobs/{job_id}/export/customer")
        assert export.status_code == 200
        workbook = load_workbook(BytesIO(export.content))
        sheet = workbook["询价单"]
        header_values = [cell.value for cell in sheet[1]]
        for label in ("数量", "报价1单价", "总价", "报价1品牌", "报价1制造商", "报价1型号",
                      "报价2单价", "报价2制造商", "报价3单价", "报价3制造商"):
            assert label in header_values
        # 复用原表已有的 单位/数量 列，不追加重复列（旧版会追加重 数量/单位）
        normalized = [str(v).strip() for v in header_values if v]
        duplicated = [x for x in set(normalized) if normalized.count(x) > 1]
        assert not duplicated, f"导出表头出现重复列: {duplicated}"
        # 数量 / 报价1单价 / 总价 written for the confirmed line (row 2)
        quantity_col = header_values.index("数量") + 1
        price_col = header_values.index("报价1单价") + 1
        total_col = header_values.index("总价") + 1
        quantity = sheet.cell(2, quantity_col).value
        price = sheet.cell(2, price_col).value
        total = sheet.cell(2, total_col).value
        assert quantity == 2
        assert price and total == round(price * quantity, 2)
        # 税后单价 / 税后总价（默认税率 10%）随导出一起生成
        assert "税后单价" in header_values and "税后总价" in header_values
        tax_unit_col = header_values.index("税后单价") + 1
        tax_total_col = header_values.index("税后总价") + 1
        tax_unit = sheet.cell(2, tax_unit_col).value
        tax_total = sheet.cell(2, tax_total_col).value
        assert tax_unit == round(price * 1.1, 2)
        assert tax_total == round(price * 1.1 * quantity, 2)
        # 产品编码列存在（本测试无编码记录，应为空字符串即可，但列必须出现）
        assert "报价1产品编码" in header_values

        # 报价2/报价3 必须与"报价1"一样带齐描述列：旧版只有 单价+制造商，
        # 导致多方案报价里后续方案没有品牌/型号/产品编码/规格。
        for prefix in ("报价2", "报价3"):
            for suffix in ("单价", "制造商", "品牌", "型号", "产品编码", "规格"):
                assert f"{prefix}{suffix}" in header_values, f"缺少列 {prefix}{suffix}"

        # 后续方案的取值必须来自对应方案本身
        detail_line = client.get(f"/api/quote-lines/{line['id']}").json()
        selected_options = sorted(
            (item for item in detail_line["options"] if item["selected"]),
            key=lambda item: item["rank"],
        )
        assert len(selected_options) == 3
        for index, option in enumerate(selected_options[1:], start=2):
            brand_col = header_values.index(f"报价{index}品牌") + 1
            maker_col = header_values.index(f"报价{index}制造商") + 1
            assert (sheet.cell(2, brand_col).value or "") == (option["record"]["brand"] or "")
            assert (sheet.cell(2, maker_col).value or "") == (option["record"]["manufacturer"] or "")
        # 至少有一个后续方案带出了非空描述信息，证明列组确实被写入而非空列
        assert any(
            (sheet.cell(2, header_values.index(f"报价{index}品牌") + 1).value or "")
            or (sheet.cell(2, header_values.index(f"报价{index}制造商") + 1).value or "")
            for index in (2, 3)
        )

        detail = workbook["多方案报价明细"]
        detail_headers = [cell.value for cell in detail[1]]
        assert "数量" in detail_headers
        assert "小计" in detail_headers
        subtotal_col = detail_headers.index("小计") + 1
        price_col_d = detail_headers.index("报价") + 1
        first_data = detail[2]
        assert first_data[subtotal_col - 1].value == round(
            first_data[price_col_d - 1].value * 2, 2
        )

        internal = client.post(f"/api/quote-jobs/{job_id}/export/internal")
        assert internal.status_code == 200
        internal_wb = load_workbook(BytesIO(internal.content))
        internal_headers = [cell.value for cell in internal_wb["询价单"][1]]
        assert "匹配状态" in internal_headers
        assert "报价2单价" in internal_headers


def test_export_skips_empty_extra_option_columns():
    """方案数填 3、整单实际只选 1 个方案时，不应留下一整组空列。"""
    with TestClient(app) as client:
        login(client)
        job_id = create_job(client, ordinary_customer_id(client), option_count=3)
        lines = client.get(f"/api/quote-jobs/{job_id}/lines").json()
        line = client.get(f"/api/quote-lines/{lines['items'][0]['id']}").json()
        assert line["options"]
        update_response = client.patch(
            f"/api/quote-lines/{line['id']}",
            json={
                "selected_option_ids": [line["options"][0]["id"]],
                "final_prices": {},
                "manual_note": "",
            },
        )
        assert update_response.status_code == 200, update_response.text

        export = client.post(f"/api/quote-jobs/{job_id}/export/customer")
        assert export.status_code == 200
        workbook = load_workbook(BytesIO(export.content))
        header_values = [cell.value for cell in workbook["询价单"][1]]
        assert "报价2单价" not in header_values
        assert "报价2品牌" not in header_values
        assert "报价3单价" not in header_values


def test_export_extra_options_follow_confirmation_state():
    """客户版对未确认行不写价，"报价2/报价3"列组必须与"报价1"同一口径。

    回归点：旧实现只给"报价1"加了 line.confirmed 判断，备选方案列无条件写入，
    结果未确认行的候选价会提前出现在客户版询价单里。
    """
    with TestClient(app) as client:
        login(client)
        content = workbook_bytes([
            [1, "电子天平", "100g，0.001g，带防风罩", "", "台", 2],
            [2, "电子天平", "200g，0.001g，带防风罩", "", "台", 3],
        ])
        job_id = create_job(client, ordinary_customer_id(client), content, option_count=3)
        lines = client.get(f"/api/quote-jobs/{job_id}/lines").json()["items"]
        assert len(lines) >= 2, "该夹具需要至少两行才能区分已确认/未确认"

        def pick_three(line_id: int):
            detail = client.get(f"/api/quote-lines/{line_id}").json()
            chosen = [item["id"] for item in detail["options"]][:3]
            assert len(chosen) == 3, "需要至少 3 个候选方案"
            response = client.patch(
                f"/api/quote-lines/{line_id}",
                json={"selected_option_ids": chosen, "final_prices": {}, "manual_note": ""},
            )
            assert response.status_code == 200, response.text

        confirmed_id = lines[0]["id"]
        pending_id = lines[1]["id"]
        pick_three(confirmed_id)
        pick_three(pending_id)
        # 只确认第一行，第二行保持未确认
        assert client.post(
            f"/api/quote-lines/{confirmed_id}/confirm", json={"override_reason": "测试"}
        ).status_code == 200

        export = client.post(f"/api/quote-jobs/{job_id}/export/customer")
        assert export.status_code == 200
        sheet = load_workbook(BytesIO(export.content))["询价单"]
        header_values = [cell.value for cell in sheet[1]]
        # 已确认行需要 3 个方案，所以列组必须存在
        for suffix in ("单价", "制造商", "品牌", "型号", "产品编码", "规格"):
            assert f"报价2{suffix}" in header_values, f"缺少列 报价2{suffix}"

        def row_for(source_row: int):
            """导出保留原表行号：表头在第 1 行，数据行号即原表行号。"""
            return source_row

        confirmed_row = row_for(lines[0]["source_row"])
        pending_row = row_for(lines[1]["source_row"])
        price_col = header_values.index("报价2单价") + 1

        assert sheet.cell(confirmed_row, price_col).value not in (None, ""), "已确认行应写入报价2单价"
        assert sheet.cell(pending_row, price_col).value in (None, ""), (
            "未确认行不应在客户版写入报价2单价"
        )
        # 报价1 的口径不变，作为对照
        primary_col = header_values.index("报价1单价") + 1
        assert sheet.cell(pending_row, primary_col).value in (None, "")

        # 内部复核版对所有行都写出，两行都应有值
        internal = client.post(f"/api/quote-jobs/{job_id}/export/internal")
        assert internal.status_code == 200
        internal_sheet = load_workbook(BytesIO(internal.content))["询价单"]
        internal_headers = [cell.value for cell in internal_sheet[1]]
        internal_price_col = internal_headers.index("报价2单价") + 1
        assert internal_sheet.cell(pending_row, internal_price_col).value not in (None, ""), (
            "内部复核版应写出未确认行的备选方案价"
        )


def test_job_rename_and_delete():
    with TestClient(app) as client:
        login(client)
        job_id = create_job(client, ordinary_customer_id(client))
        renamed = client.patch(f"/api/quote-jobs/{job_id}", json={"display_name": "重命名任务"})
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "重命名任务"
        detail = client.get(f"/api/quote-jobs/{job_id}").json()
        assert detail["display_name"] == "重命名任务"
        listing = client.get("/api/quote-jobs").json()
        assert next(item for item in listing if item["id"] == job_id)["display_name"] == "重命名任务"

        deleted = client.delete(f"/api/quote-jobs/{job_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/quote-jobs/{job_id}").status_code == 404
        assert client.delete(f"/api/quote-jobs/{job_id}").status_code == 404


def test_customer_patch_and_delete():
    with TestClient(app) as client:
        login(client)
        created = client.post("/api/customers", json={"name": "转换测试客户", "customer_type": "ordinary"})
        assert created.status_code == 200
        customer_id = created.json()["id"]

        # ordinary -> special without requirements is rejected
        rejected = client.patch(f"/api/customers/{customer_id}", json={"customer_type": "special"})
        assert rejected.status_code == 400

        # ordinary -> special with requirements
        updated = client.patch(
            f"/api/customers/{customer_id}",
            json={
                "customer_type": "special",
                "requirements": [{"attribute_name": "材质", "operator": "contains", "value": "不锈钢"}],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["customer_type"] == "special"
        assert len(updated.json()["requirements"]) == 1

        # special -> vip keeps the requirements (VIP may carry requirements)
        vip = client.patch(f"/api/customers/{customer_id}", json={"customer_type": "vip"})
        assert vip.status_code == 200
        assert vip.json()["customer_type"] == "vip"
        assert len(vip.json()["requirements"]) == 1

        # requirements array is replaced wholesale
        replaced = client.patch(
            f"/api/customers/{customer_id}",
            json={"requirements": [
                {"attribute_name": "量程", "operator": ">=", "value": "100", "unit": "g"},
                {"attribute_name": "校准", "operator": "contains", "value": "外校", "required": False},
            ]},
        )
        assert replaced.status_code == 200
        assert [item["attribute_name"] for item in replaced.json()["requirements"]] == ["量程", "校准"]

        # duplicate name rejected
        other = client.post("/api/customers", json={"name": "占用名称客户"})
        assert other.status_code == 200
        conflict = client.patch(f"/api/customers/{other.json()['id']}", json={"name": "转换测试客户"})
        assert conflict.status_code == 409

        # soft delete
        deleted = client.delete(f"/api/customers/{customer_id}")
        assert deleted.status_code == 200
        names = [item["name"] for item in client.get("/api/customers").json()]
        assert "转换测试客户" not in names
        assert client.delete(f"/api/customers/{customer_id}").status_code == 404


def test_vip_customer_can_carry_requirements_and_they_apply():
    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/customers",
            json={
                "name": "VIP带要求客户",
                "customer_type": "vip",
                "requirements": [
                    {"attribute_name": "定制属性", "operator": "contains", "value": "不可能存在的值ZZZ"}
                ],
            },
        )
        assert created.status_code == 200, created.text
        assert len(created.json()["requirements"]) == 1
        job_id = create_job(client, created.json()["id"])
        lines = client.get(f"/api/quote-jobs/{job_id}/lines").json()
        detail = client.get(f"/api/quote-lines/{lines['items'][0]['id']}").json()
        assert any(
            "未满足客户要求" in warning
            for option in detail["options"]
            for warning in option["warnings"]
        )


def test_dedup_history_redirects_option_references():
    with TestClient(app) as client:
        login(client)
        job_id = create_job(client, ordinary_customer_id(client))
        lines = client.get(f"/api/quote-jobs/{job_id}/lines").json()
        detail = client.get(f"/api/quote-lines/{lines['items'][0]['id']}").json()
        option_id = detail["options"][0]["id"]

        db = SessionLocal()
        try:
            keep = HistoryQuote(
                id="manual:dedup-keep",
                source_file="手工录入",
                name="去重测试仪器",
                normalized_name=normalize_text("去重测试仪器"),
                spec="精度0.1g",
                normalized_spec=normalize_text("精度0.1g"),
                manufacturer="测试厂",
                unit="台",
                normalized_unit=normalize_text("台"),
                price=100.0,
            )
            dup = HistoryQuote(
                id="manual:dedup-dup",
                source_file="手工录入",
                name="去重测试仪器（ ）",
                normalized_name=normalize_text("去重测试仪器（ ）"),
                spec="精度0.1g",
                normalized_spec=normalize_text("精度0.1g"),
                manufacturer="测试厂",
                unit="台",
                normalized_unit=normalize_text("台"),
                price=100.0,
            )
            db.add_all([keep, dup])
            db.execute(update(QuoteOption).where(QuoteOption.id == option_id).values(history_quote_id=dup.id))
            db.commit()
        finally:
            db.close()

        response = client.post("/api/governance/dedup-history")
        assert response.status_code == 200, response.text
        assert response.json()["removed"] == 1

        db = SessionLocal()
        try:
            assert db.get(HistoryQuote, "manual:dedup-dup") is None
            assert db.get(HistoryQuote, "manual:dedup-keep") is not None
            option = db.get(QuoteOption, option_id)
            assert option.history_quote_id == "manual:dedup-keep"
        finally:
            db.close()

        # second run is a no-op
        again = client.post("/api/governance/dedup-history")
        assert again.json()["removed"] == 0


def test_dedup_history_requires_admin():
    with TestClient(app) as client:
        login(client, "quote", "quote123")
        assert client.post("/api/governance/dedup-history").status_code == 403


def test_change_password():
    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/users",
            json={"username": "改密测试员", "password": "initial1", "display_name": "改密测试", "role": "quote"},
        )
        assert created.status_code == 201, created.text

        with TestClient(app) as user_client:
            login(user_client, "改密测试员", "initial1")
            wrong = user_client.post(
                "/api/auth/change-password", json={"old_password": "bad", "new_password": "newpass1"}
            )
            assert wrong.status_code == 400
            short = user_client.post(
                "/api/auth/change-password", json={"old_password": "initial1", "new_password": "abc"}
            )
            assert short.status_code == 422
            ok = user_client.post(
                "/api/auth/change-password", json={"old_password": "initial1", "new_password": "newpass1"}
            )
            assert ok.status_code == 200

        with TestClient(app) as relogin:
            assert relogin.post(
                "/api/auth/login", json={"username": "改密测试员", "password": "initial1"}
            ).status_code == 401
            login(relogin, "改密测试员", "newpass1")


def test_user_management():
    with TestClient(app) as client:
        login(client)
        users = client.get("/api/users").json()
        assert any(item["username"] == "admin" for item in users)
        assert all("password_hash" not in item for item in users)

        created = client.post(
            "/api/users",
            json={"username": "管理测试员", "password": "pass1234", "display_name": "管理测试", "role": "quote"},
        )
        assert created.status_code == 201, created.text
        target = created.json()
        assert target["active"] is True

        duplicate = client.post(
            "/api/users", json={"username": "管理测试员", "password": "pass1234", "role": "quote"}
        )
        assert duplicate.status_code == 409

        # admin resets password
        reset = client.patch(f"/api/users/{target['id']}", json={"password": "reset999"})
        assert reset.status_code == 200
        with TestClient(app) as target_client:
            login(target_client, "管理测试员", "reset999")

        # deactivate blocks further API use
        deactivated = client.patch(f"/api/users/{target['id']}", json={"active": False})
        assert deactivated.status_code == 200
        assert deactivated.json()["active"] is False
        with TestClient(app) as target_client:
            login(target_client, "管理测试员", "reset999")
            assert target_client.get("/api/customers").status_code == 403

        # cannot deactivate or demote self
        me = client.get("/api/auth/me").json()
        assert client.patch(f"/api/users/{me['id']}", json={"active": False}).status_code == 400
        assert client.patch(f"/api/users/{me['id']}", json={"role": "quote"}).status_code == 400

        # role change allowed for others
        promoted = client.patch(f"/api/users/{target['id']}", json={"role": "admin", "active": True})
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "admin"


def test_user_management_requires_admin():
    with TestClient(app) as client:
        login(client, "quote", "quote123")
        assert client.get("/api/users").status_code == 403
        assert client.post(
            "/api/users", json={"username": "x1", "password": "pass1234"}
        ).status_code == 403


def test_manual_history_entry():
    with TestClient(app) as client:
        login(client)
        created = client.post(
            "/api/history",
            json={
                "name": "手工录入测试仪器",
                "spec": "量程500g",
                "unit": "台",
                "manufacturer": "手工测试厂",
                "price": 888.5,
                "brand": "手工牌",
                "model": "SG-500",
                "quote_date": "2026-08",
            },
        )
        assert created.status_code == 201, created.text
        record = created.json()
        assert record["id"].startswith("manual:")
        assert record["source"] == "手工录入"

        found = client.get("/api/history/search", params={"q": "手工录入测试仪器"}).json()
        assert any(item["id"] == record["id"] for item in found)

        invalid = client.post("/api/history", json={"name": "坏记录", "price": -1})
        assert invalid.status_code == 422


def test_manual_history_entry_requires_admin():
    with TestClient(app) as client:
        login(client, "quote", "quote123")
        response = client.post("/api/history", json={"name": "越权记录", "price": 10})
        assert response.status_code == 403


def _insert_history(records: list[dict], prefix: str = "test-history") -> None:
    """直接往测试库插历史价目记录（派生列由 build_history_row 统一算）。"""
    from app.history_rows import build_history_row

    with SessionLocal() as db:
        for index, record in enumerate(records, start=1):
            db.add(build_history_row(
                record.get("id") or f"{prefix}.xlsx:测试:{index}",
                source_file=record.get("source_file", f"{prefix}.xlsx"),
                source_sheet="测试",
                source_row=index,
                name=record["name"],
                spec=record.get("spec", ""),
                product_code=record.get("product_code", ""),
                model=record.get("model", ""),
                brand=record.get("brand", ""),
                manufacturer=record.get("manufacturer", ""),
                unit=record.get("unit", "台"),
                quantity=1,
                price=record.get("price", 100.0),
                quote_date="2026-01-01",
            ))
        db.commit()


def test_selected_options_follow_manufacturer_appearance_order():
    """报价1/2/3 按厂商在价目库中的首现顺序固定，而非匹配分数先后。"""
    with TestClient(app) as client:
        login(client)
        # 乙厂先入库（首现顺序靠前），甲厂后入库但参数与询价完全一致（分数更高）；
        # 旧逻辑按分数会把甲厂排到报价1，新逻辑必须保持乙厂在前。
        _insert_history([
            {"name": "顺序验证仪", "spec": "量程100g", "manufacturer": "乙厂", "brand": "乙", "price": 100.0},
            {"name": "顺序验证仪", "spec": "量程100g，精度0.001g", "manufacturer": "甲厂", "brand": "甲", "price": 100.0},
        ], prefix="mfr-order")
        content = workbook_bytes([[1, "顺序验证仪", "量程100g，精度0.001g", "", "台", 1]])
        job_id = create_job(client, ordinary_customer_id(client), content, option_count=3)
        lines = client.get(f"/api/quote-jobs/{job_id}/lines").json()
        line = client.get(f"/api/quote-lines/{lines['items'][0]['id']}").json()
        selected = sorted((o for o in line["options"] if o["selected"]), key=lambda o: o["rank"])
        assert len(selected) >= 2
        assert selected[0]["record"]["manufacturer"] == "乙厂"
        assert selected[1]["record"]["manufacturer"] == "甲厂"


def test_export_model_falls_back_to_history_model_field():
    """历史记录无产品编码时，型号列回填型号字段（XJHY 11 位课标编码场景）。"""
    with TestClient(app) as client:
        login(client)
        _insert_history([
            {"name": "型号验证仪", "spec": "量程200g", "manufacturer": "甲厂", "brand": "甲",
             "model": "30802000503", "product_code": "", "price": 88.0},
        ], prefix="model-fallback")
        content = workbook_bytes([[1, "型号验证仪", "量程200g", "", "台", 1]])
        job_id = create_job(client, ordinary_customer_id(client), content, option_count=1)
        response = client.post(f"/api/quote-jobs/{job_id}/export/internal")
        assert response.status_code == 200, response.text
        workbook = load_workbook(BytesIO(response.content))
        sheet = workbook["询价单"]
        headers = [cell.value for cell in sheet[1]]
        model_col = headers.index("报价1型号") + 1
        code_col = headers.index("报价1产品编码") + 1
        assert sheet.cell(2, model_col).value == "30802000503"
        assert not sheet.cell(2, code_col).value
