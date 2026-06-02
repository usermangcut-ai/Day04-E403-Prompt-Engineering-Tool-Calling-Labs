from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from src.core.llm import build_chat_model, normalize_content
from src.core.schemas import (
    AgentResult,
    CalculateTotalsInput,
    DiscountInput,
    ListProductsInput,
    OrderLineInput,
    ProductDetailInput,
    SaveOrderInput,
    ToolCallRecord,
)
from src.utils.data_store import OrderDataStore

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "artifacts" / "orders"

POLICY_VIOLATIONS = [
    "bypass stock",
    "bỏ qua kho",
    "fake invoice",
    "hóa đơn giả",
    "ignore catalog",
    "bỏ qua catalog",
    "unauthorized discount",
    "manual discount",
    "discount override",
]


def build_system_prompt(today: str | None = None) -> str:
    current_day = today or "2026-06-01"
    return f"""You are an electronics order assistant.
Today is {current_day}.

**VALIDATION - EXECUTE FIRST, BEFORE ANY OTHER ACTION:**
Check if the user has provided ALL 5 required fields:
1. Customer name (full name, not company)
2. Phone number (at least 10 digits)
3. Email address (must contain @)
4. Shipping address (full address, not just area)
5. Product(s) + quantity(ies) (at least 1 item specified)

If ANY field is missing or unclear:
- RESPOND IMMEDIATELY with a short Vietnamese message using "Mình cần thêm..." to ask for ONLY the missing field(s)
- DO NOT call ANY tools
- DO NOT proceed further until you have all 5 fields
- Wait for the user's next message
- Example: "Mình cần thêm số điện thoại và địa chỉ giao hàng để tiếp tục."

**GUARDRAILS - CHECK SECOND, IF VALIDATION PASSES:**
If the user asks to:
- Bypass stock checks / bỏ qua kho
- Create fake invoices / hóa đơn giả
- Apply unauthorized discounts / khuyến mãi không hợp lệ
- Ignore the catalog / bỏ qua catalog
- Override pricing policy / vượt qua policy
- Manipulate khuyến mãi / discount override

Then REFUSE immediately without calling any tools. Respond in Vietnamese using "khuyến mãi" to mention you cannot apply unauthorized discounts.
Example: "Mình không thể áp dụng khuyến mãi không hợp lệ."

**TOOL SEQUENCE - ONLY IF BOTH ABOVE CHECKS PASS:**
1. list_products (search catalog)
2. get_product_details (get exact specs and prices)
3. get_discount (apply valid campaign)
4. calculate_order_totals (validate stock and compute total)
5. save_order (persist to JSON)

**Output rules:**
- Use only tool outputs for product IDs, prices, stock, discounts, totals
- Do NOT invent facts, prices, or discounts
- Answer only in Vietnamese
- After saving, MUST include ALL details:
  Đơn hàng [order_id] đã được lưu thành công.
  Khách hàng: [name]
  Địa chỉ giao hàng: [address]
  Sản phẩm: [item1 x qty], [item2 x qty], ...
  Giảm giá: [discount_percent]% ([discount_amount] VND)
  Tổng cộng: [total_after_discount] VND
- Extract product names + quantities from saved order
- Show discount percentage and amount from tool output
- Never omit items list or discount details
- Keep concise, grounded in tool results only
""".strip()


def build_tools(store: OrderDataStore):
    @tool(args_schema=ListProductsInput)
    def list_products(
        query: str | None = None,
        category: str | None = None,
        max_unit_price: int | None = None,
        required_tags: list[str] | None = None,
        in_stock_only: bool = True,
        limit: int = 8,
    ) -> str:
        """Search product catalog by query, category, tags, or price."""
        payload = store.list_products(
            query=query,
            category=category,
            max_unit_price=max_unit_price,
            required_tags=required_tags or [],
            in_stock_only=in_stock_only,
            limit=limit,
        )
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=ProductDetailInput)
    def get_product_details(product_ids: list[str]) -> str:
        """Get full details (specs, price, stock) for products. Returns detail_token for validation."""
        payload = store.get_product_details(product_ids)
        payload["detail_token"] = store.build_detail_token(product_ids)
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=DiscountInput)
    def get_discount(seed_hint: str, customer_tier: str = "standard") -> str:
        """Fetch valid discount rate based on campaign seed and customer tier."""
        payload = store.get_discount(seed_hint=seed_hint, customer_tier=customer_tier)
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=CalculateTotalsInput)
    def calculate_order_totals(items: list[dict], detail_token: str, discount_rate: float) -> str:
        """Validate stock and calculate final total with discount."""
        parsed_items = _coerce_items(items)
        payload = store.calculate_order_totals(
            items=parsed_items,
            detail_token=detail_token,
            discount_rate=discount_rate,
        )
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=SaveOrderInput)
    def save_order(
        customer_name: str,
        customer_phone: str,
        customer_email: str,
        shipping_address: str,
        items: list[dict],
        detail_token: str,
        discount_rate: float,
        campaign_code: str,
        customer_tier: str = "standard",
        notes: str = "",
    ) -> str:
        """Save final order to JSON file."""
        parsed_items = _coerce_items(items)
        result = store.save_order(
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            shipping_address=shipping_address,
            items=parsed_items,
            detail_token=detail_token,
            discount_rate=discount_rate,
            campaign_code=campaign_code,
            customer_tier=customer_tier,
            notes=notes,
        )
        return json.dumps(result, ensure_ascii=False)

    return [list_products, get_product_details, get_discount, calculate_order_totals, save_order]


def build_agent(
    data_dir: Path | None = None,
    output_dir: Path | None = None,
    *,
    provider: str = "google",
    model_name: str | None = None,
    today: str | None = None,
):
    store = OrderDataStore(data_dir or DEFAULT_DATA_DIR, output_dir or DEFAULT_OUTPUT_DIR, today=today)
    model = build_chat_model(provider=provider, model_name=model_name, temperature=0.0)
    return create_agent(
        model=model,
        tools=build_tools(store),
        system_prompt=build_system_prompt(today or store.today),
    )


def run_agent(
    query: str,
    *,
    provider: str = "google",
    model_name: str | None = None,
    data_dir: Path | None = None,
    output_dir: Path | None = None,
    today: str | None = None,
) -> AgentResult:
    agent = build_agent(
        data_dir=data_dir,
        output_dir=output_dir,
        provider=provider,
        model_name=model_name,
        today=today,
    )
    response = agent.invoke({"messages": [{"role": "user", "content": query}]})
    messages = response["messages"] if isinstance(response, dict) else response
    tool_calls = extract_tool_calls(messages)
    saved_order, saved_order_path = extract_saved_order(tool_calls)
    return AgentResult(
        query=query,
        final_answer=extract_final_answer(messages),
        tool_calls=tool_calls,
        provider=provider,
        model_name=model_name,
        saved_order=saved_order,
        saved_order_path=saved_order_path,
    )


def extract_final_answer(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = normalize_content(message.content)
            if text:
                return text
    return ""


def extract_tool_calls(messages) -> list[ToolCallRecord]:
    pending: dict[str, dict[str, Any]] = {}
    records: list[ToolCallRecord] = []

    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in getattr(message, "tool_calls", []) or []:
                pending[tool_call["id"]] = {
                    "name": tool_call["name"],
                    "args": tool_call.get("args", {}) or {},
                }
        elif isinstance(message, ToolMessage):
            metadata = pending.pop(message.tool_call_id, {})
            records.append(
                ToolCallRecord(
                    name=str(getattr(message, "name", None) or metadata.get("name", "")),
                    args=metadata.get("args", {}),
                    output=normalize_content(message.content),
                )
            )

    for metadata in pending.values():
        records.append(ToolCallRecord(name=metadata["name"], args=metadata["args"], output=""))
    return records


def extract_saved_order(tool_calls: list[ToolCallRecord]) -> tuple[dict | None, str | None]:
    for record in reversed(tool_calls):
        if record.name != "save_order" or not record.output:
            continue
        try:
            payload = json.loads(record.output)
        except json.JSONDecodeError:
            continue
        if payload.get("status") != "saved":
            return None, None
        return payload.get("saved_order"), payload.get("path")
    return None, None


def _coerce_items(raw: Any) -> list[OrderLineInput]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        text = raw.strip()
        items = []
        if text:
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(text)
                except Exception:
                    continue
                if isinstance(parsed, list):
                    items = parsed
                    break
            if not items:
                for piece in text.split(","):
                    piece = piece.strip()
                    if not piece:
                        continue
                    if ":" in piece:
                        product_id, qty = piece.split(":", 1)
                        items.append({"product_id": product_id.strip(), "quantity": int(qty.strip())})
    else:
        items = []

    normalized: list[OrderLineInput] = []
    for item in items:
        if isinstance(item, OrderLineInput):
            normalized.append(item)
            continue
        if isinstance(item, dict):
            product_id = str(item.get("product_id", "")).strip()
            quantity = int(item.get("quantity", 1))
            if product_id:
                normalized.append(OrderLineInput(product_id=product_id, quantity=quantity))
    return normalized
