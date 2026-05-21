"""
/早安小龍喵：每日打卡（以台灣時間 00:00 為刷新點）。

  - 依時段切換標題：早安 / 午安 / 暮色（0~3 點禁止打卡）
  - 全域 + 本群排名
  - 累計 / 連續打卡天數
  - 咕嚕喵碎片 1000~6000 隨機獎勵（每日全域只發一次）
  - Gemini 隨機生成幸運物（失敗自動 fallback）
  - 財運 / 桃花運 / 事業運 0~150 + 綜合運勢 + 星評

重複打卡規則：
  - 同一伺服器第 2 次以上 → 顯示提醒 + 今日成績單
  - 跨到其他伺服器：第 1 次不提醒、僅顯示今日成績單；第 2 次以上才提醒
  - 不論在哪個群再打，獎勵都不會重發

/帳戶總覽：顯示咕嚕喵碎片與打卡狀態（分頁版）。

資料檔 data/morning_records.json：
{
  "day_key": "YYYY-MM-DD",                     # 當前邏輯日（台灣 00:00 為界）
  "global_order": ["uid", ...],                # 今日全域首打順序
  "guilds_order": {"gid": ["uid", ...]},       # 今日各群首打順序
  "guild_signin_count": {"gid": {"uid": n}},   # 今日同伺服器打卡次數
  "user_today": {                              # 凍結的今日成績單（重複打卡時直接顯示）
    "uid": {"signin_time": "...", "global_rank": int,
            "lucky_item": str, "coin": int,
            "money_luck": int, "love_luck": int, "work_luck": int,
            "fortune_name": str, "stars": int,
            "total_days": int, "streak": int,
            "title": str, "greeting": str, "color": int,
            "guild_id": str,                     # 補簽重繪 embed 用
            "prev_last_day": str | None,         # 補簽判定：=2 天前才允許
            "prev_streak":   int}                # 補簽復原 streak 的基準
  },
  "users": {
    "uid": {"balance": int, "total_days": int,
            "streak": int, "last_day": "YYYY-MM-DD"}
  }
}
"""
from __future__ import annotations

import asyncio
import os
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands

from utils.json_store import load_json, save_json_async
from commands._wallet import WALLET_LOCK


_FILE = os.path.join('data', 'morning_records.json')
_TZ   = timezone(timedelta(hours=8))  # 台灣時間

_MAKEUP_COST = 500  # 補簽費用（咕嚕喵碎片）

# (起時, 迄時(含), 標題, 固定招呼語, 隨機第二句清單, embed 顏色)
_PERIODS: list[tuple[int, int, str, str, list[str], discord.Color]] = [
    (4, 11,
     '早安打卡！美好的一天開始囉 ⸜(* ॑꒳ ॑* )⸝',
     '',
     [
         '新的一天也要閃閃發光呀 ✨',
         '用微笑開啟美好的一天 ☀️(๑´ㅂ`๑)',
         '伸個懶腰！今天也要加油鴨 ٩(ˊᗜˋ*)و',
         '把昨天的煩惱都格式化吧 (•̀ᴗ•́)و ̑̑',
         '帶著滿滿的正能量，出發！(๑•̀ㅂ•́)و✧',
         '呼吸新鮮空氣，今天也是幸運爆棚的一天 🍀',
         '鬧鐘響起，又是充滿希望的開始 ٩(ˊᗜˋ*)و',
         '迎接晨光！今天也要元氣滿滿喔 ( `∇´ )',
         '把生活調成自己喜歡的頻道 🎵',
         '美好的一天，從一杯香濃的咖啡開始 ☕️',
         '滿血復活！新的一天請多多指教囉 ⸜(* ॑꒳ ॑* )⸝',
         '給自己一個大大的微笑，早安！✨',
     ],
     discord.Color.gold()),
    (12, 17,
     '(๑´ㅂ`๑) 醒來變午安！雖然錯過了早餐！但是靈魂充飽電感覺超級爽啦～',
     '',
     [
         '我的時差跟世界好像不太一樣 (º﹃º)',
         '醒來直接吃午餐，省了一餐賺到了',
         '完美的睡眠，就是對週末最大的尊重✨',
         '雖然錯過了早晨，但午后的陽光剛剛好 🌤️',
         '靈魂休眠結束！午安，世界 🔋(•̀ᴗ•́)و ̑̑',
         '睡到自然醒的快樂，誰懂❤️',
         '早起的人有鳥吃，那晚起的人吃午餐 🐦',
         '成功躲過早上的所有麻煩事 ✌️(✨∀✨)',
         '午安！我的床今天抓著我不放，真的不怪我 🛌',
         '太陽都曬到屁股了，那我翻個身繼續……啊不行該醒了 🍑',
         '錯過早晨不可惜，因為精彩的午后才剛開始 🥤',
         '醒來就看到大太陽，心情直接好起來 🌞',
         '下午茶時間到！這才是我的開機時間 🍰',
     ],
     discord.Color.orange()),
    (18, 23,
     '暮色降臨！夜貓子開機了~睡飽飽就是最棒的超能力！雖然跳過了白天，但夜晚的世界依然很精彩✨',
     '',
     [
         '太陽下山，才是我的高光時刻 🌙(✧﹃ ✧)',
         '越夜越美麗！夜貓子聯誼會現在開始 ✨',
         '白天在睡覺，晚上在拯救世界 (🛠️•͈ᴗ•͈)',
         '睡飽後的夜晚，連空氣都是自由的味道 🍃',
         '完美的避開了白天的喧囂🎉',
         "晚安是別人的，我的精彩才剛要開始 (◦'⌣'◦)",
         '白天都在充電，現在是放電時間 ⚡',
         '夜幕低垂，點燃我的夜貓魂 🔥',
         '錯過了白天的太陽，但我擁有整片星空 ⭐',
         '睡飽飽的超能力，讓我在晚上閃閃發光 🌟',
         '屬於夜貓子的派對，現在才要嗨起來 🎵',
         '夜晚的寧靜，配上睡飽的靈魂🍷',
         '抱歉了白天，我真的是屬於夜晚的生物 🦇',
         '黑夜給了我黑色的眼睛，我用它來熬夜 (👁️ᴗ👁️)',
         '越晚精神越好，這到底是什麼神祕體質 🌀',
         '夜晚的世界，連風都特別溫柔 ✨',
         '滿血復活的夜貓，準備出來覓食囉 🍕',
         '跳過白天的忙碌，直接享受夜晚的精彩 ✌️',
     ],
     discord.Color.purple()),
]

# 由壞到好的運勢階梯：吉運每階佔 2 格、凶運每階佔 1 格（總計 19 格）。
# 用 (財運+桃花運+事業運)/450 對應到 ratio，再依比例落入某一階。
_FORTUNE_TIERS: list[tuple[str, int]] = [
    ('大凶', 1), ('末凶', 1), ('半凶', 1), ('小凶', 1), ('凶', 1),
    ('末小吉', 2), ('末吉', 2), ('半吉', 2), ('吉', 2),
    ('小吉', 2), ('中吉', 2), ('大吉', 2),
]
_FORTUNE_TOTAL: int = sum(w for _, w in _FORTUNE_TIERS)

_FALLBACK_SOUP = [
    # 接納/自我關懷
    '善待自己，是一切美好的起點。',
    '不必活成誰的樣子，做自己就是最美的風景。',
    '不被理解時，記得：你不需要被所有人懂，只需要被自己愛。',
    '原諒自己曾經的不完美，那是你變得更好的證據。',
    '對自己溫柔一點，畢竟你也只能陪自己走一輩子。',
    '你不需要時時刻刻都堅強，允許自己脆弱也是一種勇氣。',
    '別把自己活成別人期待的樣子，你只需要忠於自己。',
    '休息不是浪費時間，而是為了走得更遠。',
    '今天的你，已經比昨天的你更值得被肯定。',
    '不必比較，每個人有自己的節奏與花期。',
    '你不必完美，只要繼續真誠地生活就好。',
    '允許自己慢一點，世界不會因此追不上你。',
    '對自己說一聲謝謝，謝謝你一直沒有放棄。',
    '善待自己的情緒，它們都是真實活著的證據。',
    '你已經做得很好了，不要忘記也擁抱當下的自己。',
    '無論今天如何，你都值得被自己溫柔對待。',
    '愛自己不是自私，而是給世界最好的禮物。',
    '不要再為了討好別人而委屈自己。',
    '請相信，你本來就值得最好的事物。',
    '你不需要證明什麼，光是存在就已經很珍貴。',

    # 勇氣/面對
    '勇敢不是不害怕，而是即使害怕也願意往前走一步。',
    '當你不再害怕跌倒，整個世界都會為你讓出一條路。',
    '真正的勇氣，是在脆弱中依然選擇前行。',
    '害怕是正常的，但別讓它成為原地踏步的理由。',
    '你以為走不過去的坎，回頭看都是風景。',
    '只要還願意嘗試，就還沒有失敗。',
    '面對未知，保持好奇而不是恐懼。',
    '勇氣不是沒有眼淚，而是含著眼淚仍向前。',
    '別讓「萬一」綁住你，也許「萬一」是最好的開始。',
    '走出舒適圈的第一步，往往是最值得的一步。',
    '當下不踏出，永遠不知道自己能走多遠。',
    '與其後悔沒做，不如做了再說。',
    '把恐懼變成行動的燃料，你會發現自己很強。',
    '不確定的路才有可能，太確定的路只剩重複。',
    '一次小小的勇敢，能改變一整年的軌跡。',

    # 努力/成長
    '今天的努力，是明天的禮物。',
    '每一份努力，都不會被時間辜負。',
    '走得慢沒關係，只要方向是對的，遲早會抵達。',
    '你不需要一次就跑得很快，只要持續走在路上。',
    '小小的進步，累積起來就是大大的改變。',
    '默默耕耘的人，終會在不被注意時開花。',
    '每天進步一點點，就是了不起的成就。',
    '所有的努力，都在悄悄為你鋪路。',
    '把汗水留給今天，把驕傲留給未來。',
    '不必和別人比，跟昨天的自己比就夠了。',
    '當下的辛苦，是未來的養分。',
    '熬過去的不是時間，是不放棄的自己。',
    '不努力的人，最容易把不可能掛在嘴邊。',
    '世界從不辜負認真生活的人。',
    '每個堅持下來的人，都會在某個時刻謝謝自己。',
    '你播下的每一顆種子，都會在合適的時節發芽。',
    '不要小看每天 1% 的累積。',
    '練習不會立刻看到成果，但會慢慢改變你。',
    '所有閃閃發光的人，都曾在無人看見的地方努力。',
    '把今天當作未來最年輕的一天，就會更想行動。',

    # 失敗/挫折
    '每一次跌倒，都是為了讓你站得更穩。',
    '失敗不是終點，是重新出發的起點。',
    '挫折是化了妝的祝福，請別急著拒絕。',
    '走錯路也是經驗，至少你知道哪裡不是答案。',
    '受過的傷，會變成保護你的鎧甲。',
    '別讓一次失敗定義整個你。',
    '低谷的好處是，往哪走都是上坡。',
    '失敗教會你的事，比成功還多。',
    '允許自己難過，但不要停在那裡。',
    '挫折面前的眼淚，是下次更堅強的種子。',
    '人生不怕跌倒，怕的是不肯再爬起來。',
    '所有偉大的故事，都有狼狽的開頭。',
    '失敗只是還沒成功的代名詞。',
    '不完美的過程，也是值得被擁抱的。',
    '把失敗當成反饋，而不是判決。',

    # 希望/光亮
    '別讓昨天的雨，淋濕了今天的太陽。',
    '黑夜再長，總會迎來黎明；困難再大，總會被你跨過。',
    '即使前方還是黑暗，也別忘了你自己就是一束光。',
    '把希望留給今天，把焦慮留給昨天。',
    '心若向陽，無懼悲傷；步若堅定，何懼遠方。',
    '只要你不放棄，世界就不會放棄你。',
    '陰天之後，總有比平常更亮的太陽。',
    '當你看不見星星，記得你正在星星之上。',
    '別在最黑暗的時候否定自己，那只是黎明前的等待。',
    '生活的甜，往往藏在熬過去的苦裡。',
    '相信光，光就在你心裡。',
    '當你感到絕望，請記得你曾走過更難的路。',
    '希望像種子，撒在心裡，總有一天會發芽。',
    '不論天氣如何，你都能為自己撐起一片晴空。',
    '看不見的未來，也不一定是壞事。',
    '人生最好的時光，往往是你不肯放棄的那一刻之後。',
    '每一個明天，都是命運給你的新機會。',
    '只要心裡有光，再深的夜都不算長。',

    # 平靜/接受
    '生活不是等待風暴過去，而是學會在雨中跳舞。',
    '不必焦急，該來的會來，該走的會走。',
    '保留三分鐘的安靜，給疲憊的自己。',
    '世界很大，請保持柔軟，繼續發光。',
    '能讓你慌的，從來不是事情本身。',
    '深呼吸，把不該帶上的事情留在這裡。',
    '靜下來，你才聽得見自己的心跳。',
    '比起跑得快，更重要的是不迷失方向。',
    '無風的湖面，才能映照星空。',
    '與其抓住一切，不如學會輕輕放下。',
    '生活的喧囂裡，留一個安靜的角落給自己。',
    '當你停下來，世界會用另一種方式擁抱你。',
    '走慢一點，也是一種前進。',
    '允許事情不完美，是最寬厚的禮物。',
    '別被情緒帶著走，先給自己一杯溫水。',

    # 時間/耐心
    '時間不會辜負，那些一直走在路上的人。',
    '別急，最好的事物，往往最值得等待。',
    '今天的種子，明天會長成意想不到的風景。',
    '時間能稀釋的，從來都不是真正的傷。',
    '慢慢來，比較快。',
    '所有來不及的失望，都會變成下一個契機。',
    '耐心是把時間變成寶藏的魔法。',
    '別讓速度欺騙了方向。',
    '日子是用一天天活的，不是用一年年想的。',
    '長得慢的樹，往往最堅固。',
    '人生像泡茶，急不得，需要時間慢慢回甘。',
    '靜待花開，是對生命最大的信任。',
    '與時間做朋友，它會給你最好的見證。',
    '走過的每一步，都會在未來連成一條路。',
    '時間答得出來的問題，就先不要急。',

    # 改變/嘗試
    '把每一個平凡的日子，都過成自己喜歡的樣子。',
    '改變從來不嫌晚，只怕你還沒開始。',
    '生活的轉折點，往往就是你決定不再等的那一刻。',
    '試一次，總比後悔一輩子好。',
    '與其抱怨環境，不如改變心境。',
    '每一個新的開始，都是命運給的小禮物。',
    '勇於放下，才能擁有更輕盈的明天。',
    '不喜歡的事，可以改；喜歡的事，要堅持。',
    '改變的勇氣，比改變本身更珍貴。',
    '把「也許」改成「立刻」，人生就不一樣了。',
    '今天的決定，會在三個月後謝謝你。',
    '不必把自己困在舊版本，你值得每天更新。',
    '別怕重新開始，每個新開始都是進化。',
    '人生不只有一條路，多走幾條風景才精彩。',
    '當你不再害怕改變，世界就會為你重新洗牌。',

    # 感恩/連結
    '微笑是最便宜的禮物，卻能改變整個世界。',
    '感謝今天的所有遇見，無論好壞。',
    '溫柔對待身邊的人，會收到加倍的溫暖。',
    '對遇見的善意說聲謝謝，世界就會多一份美好。',
    '不要忘了感謝那個還在努力的自己。',
    '世界很冷，所以你要記得發光。',
    '幸福不一定很大，但一定要被看見。',
    '一個真誠的擁抱，可以抵過千言萬語。',
    '把愛說出口，別讓重要的人等。',
    '感激不是因為擁有很多，而是因為懂得珍惜。',
    '你身邊的每一個人，都是宇宙派來的禮物。',
    '小小的善意，能在別人心裡種出大大的花。',
    '懂得感謝的人，世界自然會偏愛你一些。',
    '別讓忙碌掩蓋了愛你的人。',
    '回家時的一句「我回來了」，就是最簡單的幸福。',

    # 夢想/未來
    '夢想不會逃跑，會逃跑的只有你。',
    '把夢想拆成每天能做的一小步。',
    '未來最美的樣子，是你正在努力靠近的方向。',
    '夢想不分大小，敢開始就值得被尊敬。',
    '把仰望變成行動，星空就會回應你。',
    '人生最遠的距離，是想到卻沒做到。',
    '相信夢想，比夢想本身更有力量。',
    '把未來寫進每天的待辦事項，它就會成為現實。',
    '別讓害怕代替了你對夢想的喜歡。',
    '世界上最迷人的事情，是把熱愛活成日常。',
    '所有偉大的旅程，都從一個小小的勇氣開始。',
    '不要把夢想藏起來，說出來它會更有重量。',
    '十年後感謝今天的自己，從今天就開始。',
    '夢想不會因為遙遠而失效，只會因為你停下而消失。',
    '寫下你的目標，宇宙就會悄悄為你鋪路。',

    # 友情/愛
    '真正的朋友，是在你不發光時也願意靠近你的人。',
    '愛不只是大事，更是日常裡的細水長流。',
    '比起被理解，更難得的是被陪伴。',
    '能讓你做自己的人，才值得花時間。',
    '真正的關心，不需要太多言語。',
    '人和人之間最珍貴的，是不必解釋也懂。',
    '比起完美的人，我們更需要真誠的人。',
    '愛是看見彼此的不完美，仍然選擇靠近。',
    '一份真心，可以走過十年也不褪色。',
    '所謂緣分，是恰好遇見對的人。',

    # 內在力量
    '你內在的力量，比你想像中強大。',
    '相信自己，是這世界給你最棒的咒語。',
    '別小看自己一個微笑的能量。',
    '你能走到今天，已經是奇蹟般的事。',
    '所有的答案，其實都藏在你心裡。',
    '不必別人肯定，你的價值你自己最清楚。',
    '允許自己平凡，才有真正的不平凡。',
    '當你開始喜歡自己，全世界都會跟著喜歡你。',
    '聽從內心的聲音，那才是你真正的方向。',
    '你比你想像中堅強得多。',
    '別讓任何人的評價定義你的人生。',
    '你的存在本身，就是一種光。',
    '相信自己會好起來，這就是最大的力量。',
    '請永遠站在自己這邊。',
    '你想成為的人，就藏在你每天的選擇裡。',

    # 小確幸
    '一杯熱茶，可以救起整個下午。',
    '走在陽光下時，記得對自己說一聲早安。',
    '早晨的第一口溫水，是最簡單的療癒。',
    '把窗戶打開，讓新的空氣陪你工作。',
    '聽一首喜歡的歌，世界就溫柔起來。',
    '今天的雲很好看，記得抬頭。',
    '吃到喜歡的甜點，就是日常的小奇蹟。',
    '一覺好眠，是身體最深的告白。',
    '今天記得喝水，記得對自己微笑。',
    '抱抱身邊的人，他可能正需要。',
    '寫下今天三件感謝的事，幸福會悄悄變多。',
    '允許自己耍廢一下，那是給靈魂的假期。',
    '出門前抬頭看天空，心情會自己變亮。',
    '把喜歡的事情排進今天，日子就值得了。',
    '即使忙碌，也要記得吃飽。',

    # 結尾語
    '願你今天比昨天更喜歡自己一點。',
    '願你的笑容比咖啡還香。',
    '願你被世界溫柔以待，也願你溫柔回應它。',
    '願你今晚的夢，溫柔像棉花糖。',
    '願你內心永遠有一束光，照亮自己也照亮別人。',
    '願你成為自己嚮往的那種大人。',
    '願你被理解，被支持，被深深地愛著。',
    '願你今天遇見的人，都帶給你好心情。',
    '願你疲憊時，總有人遞上一杯溫水。',
    '願你想做的事，都能慢慢實現。',
    '願你日常的瑣碎裡，藏著小小的閃光。',
    '願你被世界看見，也被自己看見。',
    '願你今晚睡得香，明天起得早。',
    '願你勇敢、溫柔、自由、安然。',
    '願你今天比昨天，更靠近想成為的樣子。',
]

_FALLBACK_WHISPER = [
    '今天也辛苦你了…慢慢來就好，你不需要立刻變得很好。',
    '如果今天有點累，就允許自己慢一點…沒關係的，我會陪著你。',
    '想哭就哭一下吧、不用裝堅強…喵會在這裡靜靜陪你。',
    '把昨天的疲憊放下…今天，先好好喝一杯溫水好嗎？',
    '不用一次就把所有事做完…一步一步來，我都在這。',
    '你已經很努力了…今天就允許自己，被自己抱抱吧。',
    '如果世界太吵…就把耳朵交給喵，我會替你擋一下。',
    '今天的你，不需要完美…只要好好吃飯、好好呼吸就夠了。',
    '想偷懶一下也沒關係…陽光會記得替你曬暖被子的。',
    '不用急著走向遠方…喵就在這，陪你看今天的雲。',
    '今天若覺得心裡空空的…記得我留了一個位置給你窩著。',
    '你可以難過、可以脆弱…那都不會讓你變得不好。',
    '把那些「應該」放下一下吧…現在，就只當一隻被疼愛的小貓。',
    '如果今天什麼都不想做…那就一起當一隻翻肚的貓咪好了。',
    '不需要被所有人喜歡…喵喜歡你，這樣就夠了喔。',
    '你只是有點累而已…不是不夠好。先深呼吸一下吧。',
    '想躲起來的時候…喵的懷裡永遠留著一個小角落。',
    '今天的目標…只要記得對自己溫柔一點就好。',
    '不順遂的事…就當作是去曬個太陽的小繞路吧。',
    '你不需要證明什麼…光是今天願意起床，就已經很棒了。',
    '心情灰灰的也沒關係…雲後面的太陽，一直都在。',
    '今天就把自己當成最重要的客人…好好招待一下吧。',
    '不必和昨天的自己比較…你願意醒來，就是溫柔的開始。',
    '想躲進被窩多五分鐘…那就躲吧，喵會幫你看著時間。',
    '把肩膀放鬆一點…你已經扛了很多了。',
]

_FALLBACK_LUCKY = [
    # 飲品
    '溫熱奶茶', '熱拿鐵', '黑咖啡', '可可亞',
    '冰珍珠奶茶', '熱可可', '蜂蜜檸檬', '麥茶',
    '優酪乳', '豆漿', '米漿', '養樂多',
    # 甜點
    '巧克力布丁', '草莓棒棒糖', '檸檬塔', '抹茶蛋糕',
    '銅鑼燒', '馬卡龍', '小熊軟糖', '糖葫蘆',
    '雞蛋糕', '芋頭酥', '紅豆餅', '夾心餅乾',
    # 鹹食
    '熱呼呼包子', '溫泉蛋', '飯糰', '茶葉蛋',
    '便利商店御飯糰', '便當', '玉子燒', '咖哩飯',
    # 水果
    '一顆水蜜桃', '蘋果汁', '香蕉', '草莓',
    '柳橙', '葡萄', '芒果', '小蕃茄',
    # 零食
    '洋芋片', '海苔', '魷魚絲', '牛軋糖',
    # 文具
    '彩虹原子筆', '蘋果造型橡皮擦', '療癒手帳本', '楓葉書籤',
    '幸運星貼紙', '可愛便利貼', '螢光筆', '迴紋針',
    # 飾品
    '櫻花髮夾', '黑色髮圈', '小耳環', '簡約手鍊',
    # 保養
    '潤唇膏', '護手霜', '面膜', '小瓶香水',
    '防曬乳', '濕紙巾',
    # 衣物配件
    '貓爪襪', '可愛口罩', '針織帽', '格紋圍巾',
    '透氣手套', '長條髮帶',
    # 日用品
    '保溫杯', '玻璃馬克杯', '不鏽鋼水壺', '便當盒',
    '輕便雨傘', '帆布袋', '隨身鏡', '零錢包',
    '卡套', '小錢包',
    # 3C 小物
    '無線耳機', '滑鼠墊', '隨身碟', '行動電源',
    '充電線',
    # 家居
    '抱枕', '小毛毯', '香氛蠟燭', '療癒小盆栽',
    '木質相框',
    # 運動
    '運動毛巾', '輕量瑜伽墊', '小跳繩', '運動水壺',
    # 紀念/雜貨
    '兔子玩偶', '小貓造型鑰匙圈', '可愛貼紙', '日式明信片',
    '迷你筆記本', '幸運繩手環',
]

# (類別, 範例) — 每次呼叫 Gemini 前隨機抽一類，避免結果群聚到少數熱門品項。
_LUCKY_CATEGORIES: list[tuple[str, str]] = [
    ('飲品',     '熱拿鐵、珍珠奶茶、椰子水、蜂蜜檸檬'),
    ('甜點',     '布丁、銅鑼燒、蛋塔、提拉米蘇'),
    ('鹹食',     '飯糰、可頌、咖哩飯、玉子燒'),
    ('水果',     '蘋果、葡萄、芒果、奇異果'),
    ('零食',     '洋芋片、巧克力、牛軋糖、魷魚絲'),
    ('文具',     '原子筆、便利貼、橡皮擦、書籤'),
    ('書籍刊物', '小說、漫畫、散文集、雜誌'),
    ('飾品',     '手鍊、髮圈、耳環、項鍊'),
    ('個人保養', '潤唇膏、護手霜、面膜、髮蠟'),
    ('日用品',   '保溫杯、馬克杯、口罩、雨傘'),
    ('衣物配件', '帽子、襪子、圍巾、手帕'),
    ('3C 小物', '無線耳機、滑鼠墊、隨身碟、行動電源'),
    ('家居小物', '抱枕、毛毯、香氛蠟燭、月曆'),
    ('運動用品', '瑜伽墊、運動毛巾、跳繩、水壺'),
    ('植物',     '多肉植物、鬱金香、向日葵、薄荷盆栽'),
    ('紀念小物', '明信片、貼紙、鑰匙圈、徽章'),
    ('玩具',     '魔術方塊、絨毛娃娃、撲克牌、桌遊'),
]


def _now_tw() -> datetime:
    return datetime.now(_TZ)


def _signin_day_key(now: datetime | None = None) -> str:
    """台灣 00:00 為界，回傳當日 key (YYYY-MM-DD)。"""
    return (now or _now_tw()).date().isoformat()


def _yesterday_key(day_key: str) -> str:
    return (date.fromisoformat(day_key) - timedelta(days=1)).isoformat()


def _period_info(now: datetime) -> tuple[str, str, list[str], discord.Color]:
    h = now.hour
    for start, end, title, greeting, extras, color in _PERIODS:
        if start <= h <= end:
            return title, greeting, extras, color
    return _PERIODS[0][2], _PERIODS[0][3], _PERIODS[0][4], _PERIODS[0][5]


def _ensure_today(data: dict[str, Any], day_key: str) -> None:
    """日期換了就清掉舊的 daily 統計（保留 users）。"""
    if data.get('day_key') != day_key:
        data['day_key']            = day_key
        data['global_order']       = []
        data['guilds_order']       = {}
        data['guild_signin_count'] = {}
        data['user_today']         = {}
    data.setdefault('global_order', [])
    data.setdefault('guilds_order', {})
    data.setdefault('guild_signin_count', {})
    data.setdefault('user_today', {})


def _format_signin_time(now: datetime) -> str:
    """西元年/月/日 H:MM，月日時不補零、分補零。"""
    return f'{now.year}/{now.month}/{now.day} {now.hour}:{now.minute:02d}'


def _fortune_from_ratio(ratio: float) -> str:
    """ratio ∈ [0, 1] → 對應 19 格運勢階梯，吉運每階寬 2 格、凶運每階寬 1 格。"""
    ratio = max(0.0, min(1.0, ratio))
    pos = ratio * _FORTUNE_TOTAL
    cum = 0
    for name, weight in _FORTUNE_TIERS:
        cum += weight
        if pos < cum:
            return name
    return _FORTUNE_TIERS[-1][0]


def _star_bar(score: float, total: int = 10) -> str:
    """整數星格 (四捨五入) + 「(n.n/10)」分數標示，n 保留 1 位小數。"""
    score = max(0.0, min(float(total), float(score)))
    filled = int(round(score))
    bar    = '★' * filled + '☆' * (total - filled)
    return f'{bar} ({score:.1f}/{total})'


_MORNING_GEN_MODEL = 'gemini-3.1-flash-lite'


def _is_quota_error(e: BaseException) -> bool:
    s = str(e)
    return '429' in s or 'RESOURCE_EXHAUSTED' in s or 'quota' in s.lower()


async def _morning_call_with_rotation(prompt: str, system_instruction: str,
                                      temperature: float,
                                      timeout: float = 8.0) -> str:
    """呼叫 _MORNING_GEN_MODEL，遇 429 自動 rotate_api_key 後重試。

    重試上限 = 已設定的 GEMINI_API_KEYS 數量。其他例外直接 raise。
    每次重試會抓 gemini_worker._client 最新 reference（rotate 後會換新 client）。
    """
    from google.genai import types

    import gemini_worker
    from config import GEMINI_API_KEYS

    cfg = types.GenerateContentConfig(
        temperature=temperature,
        top_p=0.95,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        system_instruction=system_instruction,
    )

    def _call() -> str:
        client = gemini_worker._client
        if client is None:
            raise RuntimeError('gemini client not initialised')
        resp = client.models.generate_content(
            model=_MORNING_GEN_MODEL, contents=prompt, config=cfg,
        )
        return (resp.text or '').strip()

    n = max(1, len(GEMINI_API_KEYS))
    last_err: BaseException | None = None
    for attempt in range(n):
        try:
            return await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout)
        except Exception as e:
            last_err = e
            if not _is_quota_error(e) or attempt == n - 1:
                raise
            print(f'[MORNING-GEN] 429 quota，rotate key (try {attempt + 1}/{n})')
            gemini_worker.rotate_api_key()
    assert last_err is not None
    raise last_err


_SOUP_SYS = (
    '你是一位充滿創意與哲理的占卜師，每次都要為使用者寫出獨一無二、'
    '風格多變的心靈雞湯，避免重複常見套話。\n'
    '輸出規則：20~50 字、繁體中文、只回覆語錄本身，'
    '不加引號、標題、署名或表情符號。'
)

_WHISPER_SYS = (
    '你是一位溫柔、細心、充滿陪伴感的貓系守護者「小龍喵」。\n'
    '請為使用者生成一段名為「微光私語」的早安暖心短句。\n'
    '\n'
    '【語氣】極具溫度、溫柔、輕柔、有包容力。像默默陪在身邊的摯友，'
    '或貼在主人懷裡撒嬌的貓咪。\n'
    '【情感】主打「情感陪伴」與「自我和解」。給予安慰、理解，'
    '與「沒關係，你已經很棒了」的安心感；不講大道理，不說強行正能量的官話。\n'
    '【主題】每次隨機從以下方向擇一切入：\n'
    '  1. 允許不完美：今天可以累、可以放空、可以不那麼努力\n'
    '  2. 自我照顧：多愛自己、溫柔對待身體與情緒\n'
    '  3. 微小陪伴：無論如何我都會陪著你\n'
    '  4. 轉念療癒：把不順遂化為「只是去曬太陽、睡個覺」的溫馨比喻\n'
    '【絕對禁止】嚴禁說教、嚴禁狼性或職場雞湯、嚴禁過於冗長的排比句。\n'
    '\n'
    '【輸出規則 — 嚴格遵守】\n'
    '- 1~2 句話，40~70 字\n'
    '- 善用「…」、「、」營造輕柔節奏\n'
    '- 繁體中文，只輸出內容本身\n'
    '- 不加引號、標題、署名、表情符號、前言或後記'
)

_LUCKY_SYS = (
    '你是占卜師，要從指定類別中為使用者挑一個「今日幸運物」。\n'
    '必須是「日常真實、容易接觸」的具體物品：'
    '便利商店、超市、學校、家裡廚房或桌面上隨手能拿到的東西，'
    '價格便宜、無需特別跑去找。\n'
    '禁止：奇幻、魔法、會發光、虛構角色周邊、扭蛋、古董、收藏品、限定品、高價精品。\n'
    '\n'
    '【輸出格式 — 嚴格遵守】\n'
    '- 只輸出「物品名詞」本身，純名詞，不加任何修飾\n'
    '- 嚴禁加形容詞、狀態描述、動詞、所有格、量詞、副詞\n'
    '- 嚴禁「隨手翻開的」「療癒系」「珍藏的」「媽媽買的」「一杯」等任何前置或後置修飾\n'
    '- 繁體中文 2~6 字\n'
    '- 不加引號、標點、表情符號、說明、換行\n'
    '\n'
    '正確範例：日曆／保溫杯／原子筆／洋芋片／黑咖啡\n'
    '錯誤範例：隨手翻開的舊日曆／療癒小日曆／一杯黑咖啡／媽媽的保溫杯'
)


async def _generate_soup() -> str:
    """讓 Gemini 生成一句心靈雞湯（正能量短語），失敗 fallback 內建清單。"""
    try:
        text = await _morning_call_with_rotation(
            prompt='請隨機生成今日的一句心靈雞湯。',
            system_instruction=_SOUP_SYS,
            temperature=2.0,
        )
        text = (text.splitlines()[0].strip(' 「」"\'：:　')
                if text else '')
        return text[:120] if text else random.choice(_FALLBACK_SOUP)
    except Exception as e:
        print(f'[SOUP] fallback ({type(e).__name__}): {e}')
        return random.choice(_FALLBACK_SOUP)


async def _generate_whisper() -> str:
    """讓 Gemini 生成一段「微光私語」（溫柔陪伴感短句），失敗 fallback 內建清單。"""
    try:
        text = await _morning_call_with_rotation(
            prompt='請隨機生成今日的一段「微光私語」。',
            system_instruction=_WHISPER_SYS,
            temperature=1.8,
        )
        text = text.strip(' 「」"\'：:　') if text else ''
        return text[:140] if text else random.choice(_FALLBACK_WHISPER)
    except Exception as e:
        print(f'[WHISPER] fallback ({type(e).__name__}): {e}')
        return random.choice(_FALLBACK_WHISPER)


async def _generate_lucky_item() -> str:
    """讓 Gemini 抽一項幸運物，逾時或失敗時 fallback 到內建清單。

    每次呼叫隨機抽一個類別塞進 prompt，避免 Gemini 收斂到少數熱門答案；
    最高 temperature 讓同類別內也有大量變化，偏向日常隨手可拿到的物品。
    """
    category, examples = random.choice(_LUCKY_CATEGORIES)
    try:
        text = await _morning_call_with_rotation(
            prompt=(
                f'從「{category}」類別，挑一項今天的幸運物。\n'
                f'同類別參考方向：{examples}（再想一個別的，不要重複範例）。'
            ),
            system_instruction=_LUCKY_SYS,
            temperature=2.0,
        )
        text = (text.splitlines()[0].strip(' 「」"\'：:。.！!?？　')
                if text else '')
        return text[:12] if text else random.choice(_FALLBACK_LUCKY)
    except Exception as e:
        print(f'[LUCKY] fallback (category={category}, {type(e).__name__}): {e}')
        return random.choice(_FALLBACK_LUCKY)


def _build_embed(today: dict[str, Any], guild_rank: int,
                 *, display_name: str, balance: int) -> discord.Embed:
    desc_lines: list[str] = []
    if today.get('greeting'):
        desc_lines.append(today['greeting'])
    if today.get('extra_line'):
        desc_lines.append(today['extra_line'])
    desc_lines += [
        '',
        f'打卡時間：**{today["signin_time"]}**',
        f'您今天是第 **{today["global_rank"]}** 位，'
        f'也是本群第 **{guild_rank}** 位起床的~',
        f'🎉 累計早安 **{today["total_days"]}** 天，'
        f'連續早安 **{today["streak"]}** 天啦~',
        f'今天你的幸運物是：**{today["lucky_item"]}**',
        f'【咕嚕喵碎片 +{today["coin"]}】',
        '--------------------------------------',
        '🌸今日心靈雞湯：',
        today.get('soup', ''),
        '--------------------------------------',
        '☕ 微光私語：',
        today.get('whisper', ''),
        '--------------------------------------',
        '少女祈禱中(~▽~")...您今天的運勢為...',
        f'財運：**{today["money_luck"]}**',
        f'桃花運：**{today["love_luck"]}**',
        f'事業運：**{today["work_luck"]}**',
        f'綜合運勢：**{today["fortune_name"]}**',
        _star_bar(today['stars']),
    ]

    embed = discord.Embed(
        title=today['title'],
        description='\n'.join(desc_lines),
        color=discord.Color(today['color']),
    )
    embed.set_footer(text=f'{display_name}　帳戶餘額：{balance} 咕嚕喵碎片')
    return embed


class MorningView(discord.ui.View):
    """打卡卡片底下的補簽按鈕。每張卡只配給原打卡者使用，24h 後自動失效。"""

    def __init__(self, uid: str):
        super().__init__(timeout=86400)
        self.uid = uid

    @discord.ui.button(
        label='昨天忘記簽到?花500咕嚕喵碎片補簽!',
        style=discord.ButtonStyle.primary,
    )
    async def makeup(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的打卡卡片喔', ephemeral=True,
            )
            return

        async with WALLET_LOCK:
            data        = load_json(_FILE)
            users       = data.setdefault('users', {})
            snap        = data.get('user_today', {}).get(self.uid)
            rec         = users.get(self.uid)
            day_key     = data.get('day_key', '')
    
            if not snap or not rec or not day_key:
                await interaction.response.send_message(
                    '找不到今天的打卡紀錄，請先 /早安小龍喵', ephemeral=True,
                )
                return
    
            prev_last_day = snap.get('prev_last_day')
            prev_streak   = int(snap.get('prev_streak', 0))
            yesterday     = _yesterday_key(day_key)
            two_days_ago  = _yesterday_key(yesterday)
    
            if prev_last_day != two_days_ago:
                if prev_last_day == yesterday:
                    msg = '你昨天本來就有打卡，不用補喔'
                elif prev_last_day is None:
                    msg = '你之前沒打卡紀錄，沒得補'
                else:
                    msg = '你已經斷超過一天了，補單一天救不回 streak ㄏㄏ'
                await interaction.response.send_message(msg, ephemeral=True)
                return
    
            balance = int(rec.get('balance', 0))
            if balance < _MAKEUP_COST:
                await interaction.response.send_message(
                    f'餘額不足，補簽需要 {_MAKEUP_COST} 碎片，你只有 {balance}',
                    ephemeral=True,
                )
                return
    
            # 套用補簽：扣費、累計+1、streak 復原為 prev_streak + 2
            # (prev_streak 是 2 天前的 streak；補上昨天 +1，再算今天 +1)
            rec['balance']    = balance - _MAKEUP_COST
            rec['total_days'] = int(rec.get('total_days', 0)) + 1
            rec['streak']     = prev_streak + 2
    
            snap['total_days']    = rec['total_days']
            snap['streak']        = rec['streak']
            snap['prev_last_day'] = yesterday    # 再按一次會被擋掉
            snap['prev_streak']   = prev_streak + 1
    
            await save_json_async(_FILE, data)

        # 重繪 embed：guild_rank 由 snap.guild_id 反查
        gid         = snap.get('guild_id', '')
        guild_order = data.get('guilds_order', {}).get(gid, [])
        guild_rank  = guild_order.index(self.uid) + 1 if self.uid in guild_order else 1
        new_embed   = _build_embed(
            snap, guild_rank,
            display_name=interaction.user.display_name,
            balance=rec['balance'],
        )

        button.disabled = True
        button.label    = '已補簽 ✓'
        await interaction.response.edit_message(embed=new_embed, view=self)
        self.stop()


class TransferView(discord.ui.View):
    """/轉帳 邀請訊息底下的同意 / 拒絕按鈕。"""

    def __init__(self, sender_id: str, recipient_id: str,
                 amount: int, sender_name: str):
        super().__init__(timeout=86400)
        self.sender_id    = sender_id
        self.recipient_id = recipient_id
        self.amount       = amount
        self.sender_name  = sender_name

    async def _only_recipient(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.recipient_id:
            await interaction.response.send_message(
                '這不是給你的轉帳邀請', ephemeral=True,
            )
            return False
        return True

    def _disable_all(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True

    @discord.ui.button(label='同意接收', style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if not await self._only_recipient(interaction):
            return

        async with WALLET_LOCK:
            data   = load_json(_FILE)
            users  = data.setdefault('users', {})
            sender = users.get(self.sender_id)
            if not sender or int(sender.get('balance', 0)) < self.amount:
                self._disable_all()
                await interaction.response.edit_message(
                    content=None,
                    embed=discord.Embed(
                        title='❌ 轉帳失敗',
                        description='發送方餘額不足',
                        color=discord.Color.red(),
                    ),
                    view=self,
                )
                self.stop()
                return
    
            recipient = users.setdefault(self.recipient_id, {
                'balance':    0,
                'total_days': 0,
                'streak':     0,
                'last_day':   None,
            })
            sender['balance']    = int(sender.get('balance', 0)) - self.amount
            recipient['balance'] = int(recipient.get('balance', 0)) + self.amount
    
            await save_json_async(_FILE, data)

        self._disable_all()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title='💸 轉帳完成',
                description=(
                    f'**{self.sender_name}** → {interaction.user.mention}\n'
                    f'金額：**{self.amount}** 咕嚕喵碎片 ✓'
                ),
                color=discord.Color.green(),
            ),
            view=self,
        )
        self.stop()

    @discord.ui.button(label='拒絕接收', style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if not await self._only_recipient(interaction):
            return
        self._disable_all()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title='💸 轉帳已拒絕',
                description=(
                    f'{interaction.user.mention} 拒絕了來自 '
                    f'**{self.sender_name}** 的 **{self.amount}** 咕嚕喵碎片轉帳'
                ),
                color=discord.Color.red(),
            ),
            view=self,
        )
        self.stop()


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='早安小龍喵', description='每日打卡')
    async def slash_morning(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=discord.Embed(description='此指令只能在伺服器中使用',
                                    color=discord.Color.red()),
                ephemeral=True,
            )
            return

        now = _now_tw()
        if now.hour < 4:
            await interaction.response.send_message(
                '還沒4點，早安個鬼<(￣^￣)>。 趕緊給我去睡覺',
            )
            return

        await interaction.response.defer()

        day_key = _signin_day_key(now)

        async with WALLET_LOCK:
            data = load_json(_FILE)
            _ensure_today(data, day_key)
    
            users        = data.setdefault('users', {})
            user_today   = data['user_today']
            global_order = data['global_order']
            guilds_order = data['guilds_order']
    
            uid = str(interaction.user.id)
            gid = str(guild.id)
    
            guild_order   = guilds_order.setdefault(gid, [])
            gid_count_map = data['guild_signin_count'].setdefault(gid, {})
            prev_count    = int(gid_count_map.get(uid, 0))
            gid_count_map[uid] = prev_count + 1
    
            is_same_guild_repeat = prev_count >= 1
            is_new_today         = uid not in user_today
    
            if is_new_today:
                title, greeting, extras, color = _period_info(now)
                extra_line = random.choice(extras) if extras else ''
    
                global_order.append(uid)
                if uid not in guild_order:
                    guild_order.append(uid)
    
                user_rec = users.setdefault(uid, {
                    'balance':    0,
                    'total_days': 0,
                    'streak':     0,
                    'last_day':   None,
                })
                # 保留打卡前的 last_day / streak，給補簽按鈕判斷與還原 streak 用
                prev_last_day = user_rec.get('last_day')
                prev_streak   = int(user_rec.get('streak', 0))
                if prev_last_day == _yesterday_key(day_key):
                    user_rec['streak'] = prev_streak + 1
                else:
                    user_rec['streak'] = 1
                user_rec['total_days'] = int(user_rec.get('total_days', 0)) + 1
                user_rec['last_day']   = day_key
    
                coin = random.randint(1000, 6000)
                user_rec['balance'] = int(user_rec.get('balance', 0)) + coin
    
                lucky, soup, whisper = await asyncio.gather(
                    _generate_lucky_item(), _generate_soup(), _generate_whisper(),
                )
    
                money_luck = random.randint(0, 150)
                love_luck  = random.randint(0, 150)
                work_luck  = random.randint(0, 150)
                ratio      = (money_luck + love_luck + work_luck) / 450.0
                fortune_name = _fortune_from_ratio(ratio)
                stars        = round(ratio * 10, 1)
    
                user_today[uid] = {
                    'signin_time':   _format_signin_time(now),
                    'global_rank':   len(global_order),
                    'lucky_item':    lucky,
                    'soup':          soup,
                    'whisper':       whisper,
                    'coin':          coin,
                    'money_luck':    money_luck,
                    'love_luck':     love_luck,
                    'work_luck':     work_luck,
                    'fortune_name':  fortune_name,
                    'stars':         stars,
                    'total_days':    user_rec['total_days'],
                    'streak':        user_rec['streak'],
                    'title':         title,
                    'greeting':      greeting,
                    'extra_line':    extra_line,
                    'color':         color.value,
                    'guild_id':      gid,
                    'prev_last_day': prev_last_day,
                    'prev_streak':   prev_streak,
                }
            elif not is_same_guild_repeat:
                # 別群第一次：加入該群順序，不領獎
                if uid not in guild_order:
                    guild_order.append(uid)
    
            today_snapshot = user_today[uid]
            guild_rank     = guild_order.index(uid) + 1 if uid in guild_order else len(guild_order)
            balance        = int(users.get(uid, {}).get('balance', 0))
    
            embed = _build_embed(
                today_snapshot,
                guild_rank,
                display_name=interaction.user.display_name,
                balance=balance,
            )
            content = '_今天已經打卡過了，這是你今天的紀錄～_' if is_same_guild_repeat else None
    
            await save_json_async(_FILE, data)
        await interaction.followup.send(
            content=content, embed=embed, view=MorningView(uid),
        )


    @tree.command(name='帳戶總覽',
                  description='查看自己或他人的帳戶總覽（錢包/銀行/股票/打卡/礦工）')
    @app_commands.describe(用戶='要查看的用戶（留空看自己）')
    async def slash_balance(
        interaction: discord.Interaction,
        用戶: discord.Member | None = None,
    ):
        target = 用戶 or interaction.user
        if target.bot:
            await interaction.response.send_message(
                '機器人沒有帳戶喵', ephemeral=True,
            )
            return

        await interaction.response.defer()

        from commands.shop import (
            MINER_CAP, REVERSE_CARD_MAX, _POTION_TIERS,
            get_tier_until, get_today_miner_gain, get_reverse_cards,
        )
        from commands.stock import build_account_pages, AccountPagerView

        uid  = str(target.id)
        rec  = load_json(_FILE).get('users', {}).get(uid, {})
        total_days   = int(rec.get('total_days', 0))
        streak       = int(rec.get('streak', 0))
        miners       = int(rec.get('miners', 0))
        today_gain   = get_today_miner_gain(rec)
        reverse_cards = get_reverse_cards(uid)

        miner_lines = [
            f'持有：**{miners}** / {MINER_CAP} 台　|　'
            f'今日收益：**{today_gain:,}** 碎片',
        ]
        for tier in ('a', 'b', 'c'):
            cfg = _POTION_TIERS[tier]
            exp = get_tier_until(rec, tier)
            if exp:
                miner_lines.append(
                    f'{cfg["emoji"]} {cfg["name"]} (×{cfg["mult"]}) 到期 '
                    f'<t:{int(exp.timestamp())}:R>'
                )

        extra = [
            (
                '📅 打卡狀態',
                f'累計：**{total_days}** 天　|　連續：**{streak}** 天',
                False,
            ),
            (
                '⛏️ 礦工',
                '\n'.join(miner_lines),
                False,
            ),
            (
                '🎴 道具',
                f'🪞 反轉牌 ({reverse_cards}/{REVERSE_CARD_MAX})',
                False,
            ),
        ]
        pages = await build_account_pages(
            target.id, target.display_name,
            title=f'🏦 {target.display_name} 的帳戶總覽',
            extra_fields=extra,
        )
        owner_uid = str(target.id) if target.id == interaction.user.id else None
        await interaction.followup.send(
            embed=pages[0],
            view=AccountPagerView(pages, page_idx=0, owner_uid=owner_uid),
        )


    @tree.command(name='轉帳', description='轉帳咕嚕喵碎片給其他用戶')
    @app_commands.describe(
        用戶='要轉給的用戶',
        金額='要轉的咕嚕喵碎片數量（必須大於 0）',
    )
    async def slash_transfer(
        interaction: discord.Interaction,
        用戶: discord.Member,
        金額: app_commands.Range[int, 1, None],
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                '此指令只能在伺服器中使用', ephemeral=True,
            )
            return
        if 用戶.bot or 用戶.id == interaction.user.id:
            await interaction.response.send_message(
                '不能轉給自己或機器人', ephemeral=True,
            )
            return

        sender_id  = str(interaction.user.id)
        data       = load_json(_FILE)
        sender_rec = data.get('users', {}).get(sender_id, {})
        balance    = int(sender_rec.get('balance', 0))
        if balance < 金額:
            await interaction.response.send_message(
                f'你的餘額不足（目前 {balance} 咕嚕喵碎片）',
                ephemeral=True,
            )
            return

        view  = TransferView(
            sender_id, str(用戶.id), int(金額),
            interaction.user.display_name,
        )
        embed = discord.Embed(
            title='💸 轉帳邀請',
            description=(
                f'**{interaction.user.display_name}** 想轉 '
                f'**{金額}** 咕嚕喵碎片給 {用戶.mention}\n\n'
                '按「同意接收」收下，或「拒絕接收」退回。'
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            content=用戶.mention, embed=embed, view=view,
        )
