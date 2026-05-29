import os
import hmac
import hashlib
import base64
import json
import re
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# ========== 環境変数 ==========
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ========== LINE署名検証 ==========
def verify_signature(body: bytes, signature: str) -> bool:
    hash = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(hash).decode()
    return hmac.compare_digest(expected, signature)

# ========== LINE返信 ==========
async def reply_text(reply_token: str, message: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "replyToken": reply_token,
                "messages": [{"type": "text", "text": message}]
            },
            timeout=5.0
        )

async def reply_flex(reply_token: str, flex_contents: dict, alt_text: str = "査定結果"):
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "replyToken": reply_token,
                "messages": [{
                    "type": "flex",
                    "altText": alt_text,
                    "contents": flex_contents
                }]
            },
            timeout=5.0
        )

# ========== 画像ダウンロード ==========
async def download_image(message_id: str) -> bytes:
    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=10.0
        )
        return res.content

# ========== Gemini解析 ==========
async def analyze_image_with_gemini(image_bytes: bytes) -> dict:
    image_b64 = base64.b64encode(image_bytes).decode()
    prompt = """この画像に写っている商品を解析してください。
以下のJSON形式のみで回答してください。他のテキストは一切不要です。

{
  "brand": "ブランド名",
  "model": "モデル名",
  "ref": "型番・Ref番号",
  "category": "カテゴリ（時計/バッグ/ジュエリー/その他）",
  "estimated_price_min": 最低価格(数値のみ),
  "estimated_price_max": 最高価格(数値のみ),
  "confidence": "high/medium/low",
  "reason": "特定できない場合の理由"
}

型番が不明な場合はrefを"unknown"にしてください。
ブランドが特定できない場合はbrandを"unknown"にしてください。"""

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}
                    ]
                }],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 500}
            },
            timeout=15.0
        )
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r'```json\s*|\s*```', '', text).strip()
        return json.loads(text)

# ========== ヤフオクスクレイピング ==========
async def search_yahooauction(query: str) -> dict:
    search_query = query.replace(" ", "+")
    url = f"https://auctions.yahoo.co.jp/search/search?p={search_query}&va={search_query}&exflg=1&b=1&n=20&s1=cbids&o1=d"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en;q=0.9"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=8.0, follow_redirects=True)
            html = res.text
            
            prices = re.findall(r'(\d{1,3}(?:,\d{3})*)\s*円', html)
            prices_int = [int(p.replace(',', '')) for p in prices if int(p.replace(',', '')) > 1000]
            
            if prices_int:
                prices_int.sort()
                mid_prices = prices_int[len(prices_int)//4 : len(prices_int)*3//4]
                avg = sum(mid_prices) // len(mid_prices) if mid_prices else sum(prices_int) // len(prices_int)
                return {
                    "success": True,
                    "avg_price": avg,
                    "min_price": min(prices_int),
                    "max_price": max(prices_int),
                    "count": len(prices_int)
                }
    except Exception:
        pass
    
    return {"success": False}

# ========== Flexメッセージ作成 ==========
def create_flex_message(gemini_result: dict, auction_result: dict) -> dict:
    brand = gemini_result.get("brand", "不明")
    model = gemini_result.get("model", "不明")
    ref = gemini_result.get("ref", "不明")
    category = gemini_result.get("category", "商品")
    est_min = gemini_result.get("estimated_price_min", 0)
    est_max = gemini_result.get("estimated_price_max", 0)

    if auction_result.get("success"):
        avg = auction_result["avg_price"]
        min_p = auction_result["min_price"]
        max_p = auction_result["max_price"]
        count = auction_result["count"]
        market_text = f"¥{avg:,} (相場: ¥{min_p:,}〜¥{max_p:,})"
        market_sub = f"ヤフオク {count}件の落札データより"
    else:
        avg = (est_min + est_max) // 2 if est_min and est_max else 0
        market_text = f"¥{avg:,} (AI推定: ¥{est_min:,}〜¥{est_max:,})" if avg else "データ取得中"
        market_sub = "AI推定価格（参考値）"

    buy_price = int(avg * 0.6) if avg else 0
    buy_text = f"¥{buy_price:,}" if buy_price else "要確認"

    return {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#FF6B35",
            "contents": [{
                "type": "text",
                "text": "🔍 査定結果",
                "color": "#FFFFFF",
                "size": "xl",
                "weight": "bold"
            }]
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F5F5F5",
                    "cornerRadius": "8px",
                    "paddingAll": "12px",
                    "contents": [
                        {"type": "text", "text": brand, "size": "xl", "weight": "bold", "color": "#1A1A1A"},
                        {"type": "text", "text": model, "size": "md", "color": "#333333", "wrap": True},
                        {"type": "text", "text": f"Ref: {ref}", "size": "sm", "color": "#888888"}
                    ]
                },
                {"type": "separator"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": "📊 市場相場", "size": "sm", "color": "#888888", "weight": "bold"},
                        {"type": "text", "text": market_text, "size": "lg", "weight": "bold", "color": "#1A1A1A"},
                        {"type": "text", "text": market_sub, "size": "xs", "color": "#AAAAAA"}
                    ]
                },
                {"type": "separator"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#FFF3E0",
                    "cornerRadius": "8px",
                    "paddingAll": "12px",
                    "contents": [
                        {"type": "text", "text": "💰 買取参考価格", "size": "sm", "color": "#E65100", "weight": "bold"},
                        {"type": "text", "text": buy_text, "size": "xxl", "weight": "bold", "color": "#E65100"},
                        {"type": "text", "text": "※状態により変動します", "size": "xs", "color": "#AAAAAA"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [{
                "type": "text",
                "text": "正確な査定はスタッフにお問い合わせください",
                "size": "xs",
                "color": "#AAAAAA",
                "wrap": True,
                "align": "center"
            }]
        }
    }

# ========== メインWebhook ==========
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")
    
    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    data = json.loads(body)
    
    for event in data.get("events", []):
        if event["type"] != "message":
            continue
        
        reply_token = event["replyToken"]
        message = event["message"]
        
        if message["type"] != "image":
            await reply_text(reply_token, "📷 査定したい商品の写真を送ってください！\n時計・ブランドバッグ・ジュエリーなど査定します。")
            continue
        
        # 即座に受付メッセージ（3秒対策）
        asyncio.create_task(process_image(reply_token, message["id"]))
        
    return JSONResponse(content={"status": "ok"})

async def process_image(reply_token: str, message_id: str):
    try:
        image_bytes = await download_image(message_id)
        gemini_result = await analyze_image_with_gemini(image_bytes)
        
        brand = gemini_result.get("brand", "unknown")
        ref = gemini_result.get("ref", "unknown")
        model = gemini_result.get("model", "")
        confidence = gemini_result.get("confidence", "low")
        if brand == "unknown":
            await reply_text(reply_token, "バイヤーが査定中です。\n少々お待ちください。スタッフよりご連絡いたします。")
            return
        
        search_query = f"{brand} {model} {ref}".replace("unknown", "").strip()
        auction_result = await search_yahooauction(search_query)
        
        flex = create_flex_message(gemini_result, auction_result)
        await reply_flex(reply_token, flex)
        
    except Exception as e:
        await reply_text(reply_token, "バイヤーが査定中です。\n少々お待ちください。スタッフよりご連絡いたします。")

# ========== ヘルスチェック ==========
@app.get("/")
async def health():
    return {"status": "ok", "service": "LINE査定Bot"}
