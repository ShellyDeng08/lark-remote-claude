import asyncio
import json
from pathlib import Path

from lark_client.oauth_service import LarkOAuthService
from lark_client.user_api import LarkUserApi
from lark_client.config import FEISHU_APP_ID, FEISHU_APP_SECRET, OAUTH_REDIRECT_URI

async def main():
    oauth_service = LarkOAuthService(FEISHU_APP_ID, FEISHU_APP_SECRET, OAUTH_REDIRECT_URI)
    user_api = LarkUserApi(oauth_service)
    
    tokens = oauth_service._load_all_tokens()
    if not tokens:
        print("没有已授权用户")
        return
    
    user_id = list(tokens.keys())[0]
    print(f"使用用户: {user_id[:8]}...")
    
    result = await user_api.get_user_chats(user_id, page_size=30)
    chats = result.get("items", [])
    
    print(f"获取到 {len(chats)} 个会话")
    
    output = {"total": len(chats), "chats": chats}
    
    output_file = Path("chats_detail_30.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"已保存到: {output_file}")
    
    print("\n前3个会话的字段：")
    for i, chat in enumerate(chats[:3]):
        print(f"\n=== 会话 {i+1} ===")
        print(f"所有字段: {list(chat.keys())}")
        for key in chat.keys():
            print(f"  {key}: {chat.get(key)}")

asyncio.run(main())
