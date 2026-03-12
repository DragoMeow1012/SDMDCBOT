"""
LINE Bot 模組：接收 LINE Webhook 事件，送入 msg_queue 讓 gemini_worker 統一處理。

啟動方式：由 main.py 透過 start_line_server() 自動啟動（設定 LINE_CHANNEL_ACCESS_TOKEN 後生效）。
Webhook URL 請設定為：https://<your-domain>:<port>/webhook
"""
import asyncio
from aiohttp import web

from config import LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET
from gemini_worker import msg_queue

# LINE Bot SDK v3 imports（需安裝 line-bot-sdk>=3.0.0）
try:
    from linebot.v3.webhook import WebhookParser
    from linebot.v3.webhooks import (
        MessageEvent, TextMessageContent, ImageMessageContent,
    )
    from linebot.v3.messaging import (
        AsyncApiClient, AsyncMessagingApi, AsyncMessagingApiBlob,
        Configuration, ReplyMessageRequest, PushMessageRequest,
        TextMessage as LineTextMessage,
    )
    from linebot.v3.exceptions import InvalidSignatureError
    _LINE_SDK_AVAILABLE = True
except ImportError:
    _LINE_SDK_AVAILABLE = False


class _NullTyping:
    """LINE 沒有 typing indicator，使用 no-op context manager 代替。"""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def _make_line_send_fn(reply_token: str, push_to: str, config: 'Configuration'):
    """
    建立 LINE 回覆函式。
    第一次呼叫優先使用 reply_token（免費），失敗或後續呼叫改用 push API。
    """
    _used_reply = False

    async def send_fn(text: str) -> None:
        nonlocal _used_reply
        # LINE 單則訊息上限 5000 字，超過則截斷
        chunks = [text[i:i + 4999] for i in range(0, len(text), 4999)]

        async with AsyncApiClient(config) as api_client:
            api = AsyncMessagingApi(api_client)

            if not _used_reply and reply_token:
                _used_reply = True
                try:
                    await api.reply_message(ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[LineTextMessage(text=c) for c in chunks[:5]],
                    ))
                    return
                except Exception as e:
                    print(f'[LINE] reply_message 失敗，改用 push: {e}')

            # fallback：push API（需 LINE 付費方案或 Developer Trial）
            for chunk in chunks:
                try:
                    await api.push_message(PushMessageRequest(
                        to=push_to,
                        messages=[LineTextMessage(text=chunk)],
                    ))
                except Exception as e:
                    print(f'[LINE] push_message 失敗: {e}')

    return send_fn


async def _handle_webhook(
    request: web.Request,
    chat_sessions: dict,
    init_session_fn,
    config: 'Configuration',
    parser: 'WebhookParser',
) -> web.Response:
    body = await request.text()
    signature = request.headers.get('X-Line-Signature', '')

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        raise web.HTTPBadRequest(text='Invalid signature')

    for event in events:
        if not isinstance(event, MessageEvent):
            continue

        source = event.source
        # 決定 session key 與 push target
        if source.type == 'user':
            push_to = source.user_id
            cid = f'line_user_{source.user_id}'
        elif source.type == 'group':
            push_to = source.group_id
            cid = f'line_group_{source.group_id}'
        elif source.type == 'room':
            push_to = source.room_id
            cid = f'line_room_{source.room_id}'
        else:
            continue

        file_parts: list[dict] = []
        prompt: str = ''

        if isinstance(event.message, TextMessageContent):
            prompt = event.message.text.strip()
            if not prompt:
                continue
            # 只有包含 @小龍喵 才回覆，並移除提及前綴
            if '@小龍喵' not in prompt:
                continue
            prompt = prompt.replace('@小龍喵', '').strip()

        elif isinstance(event.message, ImageMessageContent):
            try:
                async with AsyncApiClient(config) as api_client:
                    blob_api = AsyncMessagingApiBlob(api_client)
                    image_bytes = await blob_api.get_message_content(event.message.id)
                file_parts = [{'data': bytes(image_bytes), 'mime_type': 'image/jpeg'}]
                prompt = '請描述這個附件的內容。'
            except Exception as e:
                print(f'[LINE] 圖片下載失敗: {e}')
                continue

        else:
            continue  # 不支援的訊息類型（貼圖、語音等）

        # 初始化 session（如尚未建立）
        sess = chat_sessions.get(cid)
        if not sess or not sess.get('chat_obj'):
            init_session_fn(cid, 'general', sess)

        # LINE 來源用戶身分前綴
        user_prefix = f'[LINE User: {push_to}]\n'
        final_prompt = user_prefix + prompt

        send_fn = _make_line_send_fn(event.reply_token, push_to, config)

        await msg_queue.put({
            'channel_id': cid,
            'prompt_text': final_prompt,
            'file_parts': file_parts,
            'reply_fn': send_fn,
            'send_fn': send_fn,
            'typing_ctx': _NullTyping(),
            'kb_save': None,
        })

        print(f'[LINE] ch={cid} prompt={prompt[:60]}')

    return web.Response(text='OK')


async def start_line_server(
    chat_sessions: dict,
    knowledge_entries: list,
    port: int,
    init_session_fn,
) -> None:
    """啟動 LINE Webhook aiohttp server。"""
    if not _LINE_SDK_AVAILABLE:
        print('[LINE] line-bot-sdk 未安裝，LINE Bot 停用。請執行：pip install line-bot-sdk>=3.0.0')
        return

    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
        print('[LINE] 未設定 LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET，跳過 LINE Bot。')
        return

    config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    parser = WebhookParser(LINE_CHANNEL_SECRET)

    app = web.Application()

    async def webhook_handler(request: web.Request) -> web.Response:
        return await _handle_webhook(request, chat_sessions, init_session_fn, config, parser)

    app.router.add_post('/webhook', webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f'[LINE] Webhook server 已啟動，監聽 port {port}（路徑：/webhook）')

    # 保持運行直到外部 cancel
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        await runner.cleanup()
