#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape_imabari_news.py

今治市公式サイトの更新情報ページ（whatsnew.html）を巡回し、
過去7日分のお知らせを抽出。各お知らせの詳細ページも取得して、
タイトルと要約をどちらも読みやすい日本語に言い換えて
docs/news.json として書き出す。

言い換えは2段構え：
  1. GitHub Models（GitHub Actions内でGITHUB_TOKENだけで無料で使えるAI）
     が使えればそちらでやさしい言い換えを作る
  2. 使えない場合（GITHUB_TOKEN未設定・ローカル実行・レート制限時など）は
     ルールベースの言い換えに自動フォールバックする
どちらも追加のサインアップやクレジットカード登録は不要。

GitHub Actions などから毎日1回実行することを想定している。
実行のたびに全件を作り直すので、当日分・翌日分の反映も自動で行われる。

依存: requests, beautifulsoup4
    pip install requests beautifulsoup4
"""

import json
import os
import re
import time
import datetime
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.city.imabari.ehime.jp/whatsnew.html"
SITE_ROOT = "https://www.city.imabari.ehime.jp/"
OUTPUT_PATH = Path(__file__).parent / "docs" / "news.json"

# 今治市サイトへの負荷を抑えるための最低限のマナー設定
REQUEST_DELAY_SEC = 1.5
REQUEST_TIMEOUT = 15
HEADERS = {
    "User-Agent": "ImabariNewsBoardBot/1.0 (+personal project; contact: set-your-contact-here)"
}

DAYS_TO_KEEP = 7
MAX_SUMMARY_CHARS = 160

# --- GitHub Models（無料・サインアップ不要。GitHub Actions内でのみ有効） ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
GITHUB_MODELS_MODEL = "openai/gpt-4o-mini"

CATEGORY_RULES = [
    ("交通・航路", ["航路", "運航", "交通規制", "運休", "フェリー", "渡船"]),
    ("募集・イベント", ["募集", "開催", "講座", "研修", "説明会", "ワークショップ", "コンテスト", "フェスティバル"]),
    ("補助金・支援", ["補助", "助成", "応援金", "支援金", "給付"]),
    ("入札・プロポーザル", ["プロポーザル", "入札", "公募"]),
]

# タイトルの「お役所文型」→自然な言い方への変換ルール（GitHub Modelsが
# 使えない場合のフォールバック用）。上から順に試して最初にマッチしたもの
# だけを適用する。
TITLE_RULES = [
    (re.compile(r"「(.+?)」に係る公募型プロポーザルの実施について、?(質問及び回答|選定結果)を?掲載しました。?$"),
     lambda m: f"「{m.group(1)}」の{m.group(2)}を公開しました"),
    (re.compile(r"「(.+?)」に係る公募型プロポーザルの実施について。?$"),
     lambda m: f"「{m.group(1)}」の委託・受託事業者を募集します"),
    (re.compile(r"「(.+?)」の選定結果について。?$"),
     lambda m: f"「{m.group(1)}」の選定結果を発表しました"),
    (re.compile(r"(.+?)の選定結果について。?$"), lambda m: f"{m.group(1)}の選定結果を発表しました"),
    (re.compile(r"(.+?)の開催について。?$"), lambda m: f"{m.group(1)}を開催します"),
    (re.compile(r"(.+?)の募集について。?$"), lambda m: f"{m.group(1)}の参加者を募集します"),
    (re.compile(r"(.+?)の実施について。?$"), lambda m: f"{m.group(1)}を行います"),
    (re.compile(r"(.+?)を掲載しました。?$"), lambda m: f"{m.group(1)}を公開しました"),
    (re.compile(r"(.+?)について、?質問及び回答を掲載しました。?$"), lambda m: f"{m.group(1)}の質問・回答を公開しました"),
    (re.compile(r"(.+?)について。?$"), lambda m: f"{m.group(1)}のお知らせ"),
]

# 要約を組み立てる際に拾いたい見出しラベルと、そこに対応するキーワード
# （フォールバック用）
SUMMARY_FIELD_RULES = [
    ("対象", ["対象", "参加"]),
    ("期間・日程", ["期間", "日程", "日時", "受付期間", "実施日"]),
    ("金額", ["金額", "補助金額", "料金", "費用"]),
    ("申込方法", ["申込", "申請方法", "応募方法", "参加方法", "登録"]),
    ("問い合わせ", ["問い合わせ", "お問い合わせ"]),
]


def guess_category(title: str) -> str:
    for cat, keywords in CATEGORY_RULES:
        if any(k in title for k in keywords):
            return cat
    return "お知らせ"


def simplify_title_rule_based(title: str) -> str:
    """行政特有の言い回しを、自然な言い方に置き換える（ルールベース・無料）。"""
    t = title.strip()
    for pattern, repl in TITLE_RULES:
        m = pattern.match(t)
        if m:
            return repl(m)
    return t


def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return BeautifulSoup(resp.text, "html.parser")


def parse_whatsnew(soup: BeautifulSoup):
    """
    更新情報ページから (date, title, url) のリストを抽出する。
    サイト構造が変わった場合はここを直す。
    """
    items = []
    main = soup.find(id="main_container") or soup
    date_pattern = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

    for node in main.find_all(string=date_pattern):
        m = date_pattern.search(node)
        if not m:
            continue
        y, mo, d = map(int, m.groups())
        try:
            date = datetime.date(y, mo, d)
        except ValueError:
            continue

        link_el = node.parent.find_next("a")
        if link_el is None:
            continue

        title = link_el.get_text(strip=True)
        href = link_el.get("href", "")
        if not title or not href:
            continue
        url = urljoin(SITE_ROOT, href)
        items.append({"date": date.isoformat(), "title": title, "url": url})

    return items


def extract_lead_text(main) -> str:
    """タイトル(h1)直後の説明文だけを狙って抜き出す。"""
    h1 = main.find("h1")
    texts = []
    if h1:
        for el in h1.find_all_next():
            if el.name in ("h2", "h3"):
                break
            if el.name in ("p", "li"):
                t = el.get_text(strip=True)
                if len(t) > 15:
                    texts.append(t)
            if len(texts) >= 3:
                break
    if not texts:
        texts = [
            p.get_text(strip=True)
            for p in main.find_all(["p", "li"])
            if len(p.get_text(strip=True)) > 15
        ][:3]
    return re.sub(r"\s+", " ", " ".join(texts)).strip()


def extract_structured_summary(main) -> str | None:
    """
    ページ自身が使っている見出し（対象／期間／金額 など）を拾って、
    「対象: 〜／期間: 〜」の形に組み立てる（フォールバック用）。
    """
    found = {}
    for h in main.find_all(["h2", "h3"]):
        htext = h.get_text(strip=True)
        for label, keywords in SUMMARY_FIELD_RULES:
            if label in found:
                continue
            if any(k in htext for k in keywords):
                nxt = h.find_next(["p", "li", "table"])
                if not nxt:
                    continue
                t = nxt.get_text(" ", strip=True)
                t = re.sub(r"\s+", " ", t).strip()
                if not t:
                    continue
                if len(t) > 45:
                    t = t[:45] + "…"
                found[label] = t
                break
    if not found:
        return None
    if list(found.keys()) == ["問い合わせ"]:
        return None
    return "　".join(f"{k}：{v}" for k, v in found.items())


def rewrite_with_github_models(title: str, lead_text: str):
    """
    GitHub Models（無料・GITHUB_TOKENのみで利用可）で、タイトルと要約を
    まとめてやさしい日本語に言い換える。GITHUB_TOKENが無い場合や、
    呼び出しに失敗した場合は None を返す（呼び出し側でフォールバックする）。
    """
    if not GITHUB_TOKEN or not lead_text:
        return None

    prompt = (
        "あなたは自治体広報の編集者です。以下は今治市公式サイトのお知らせの"
        "タイトルと本文抜粋です。専門用語や「〜について」「〜に係る」のような"
        "硬い言い回しを避け、一般市民が一読して内容と自分に関係あるかが"
        "分かるように書き直してください。日付・金額・締切など具体的な数字は"
        "省略せず残してください。\n\n"
        f"【元のタイトル】{title}\n【本文抜粋】{lead_text}\n\n"
        "次のJSON形式のみで出力してください（前置き・コードブロック記号は不要）：\n"
        '{"title": "やさしい言い換えタイトル（30字程度）", '
        '"summary": "やさしい要約（2文以内・120字程度）"}'
    )

    try:
        resp = requests.post(
            GITHUB_MODELS_URL,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
            },
            json={
                "model": GITHUB_MODELS_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
        parsed = json.loads(content)
        plain_title = parsed.get("title", "").strip()
        plain_summary = parsed.get("summary", "").strip()
        if plain_title and plain_summary:
            return plain_title, plain_summary
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] GitHub Models呼び出しに失敗（ルールベースを使用します）: {e}", file=sys.stderr)
        return None


def rewrite_detail_page(url: str, official_title: str):
    """
    詳細ページを取得し、(表示用タイトル, 要約) を返す。
    PDFや外部サイトは本文取得をスキップする。
    """
    if url.lower().endswith(".pdf"):
        return simplify_title_rule_based(official_title), "（PDF資料）詳細は今治市サイトのPDFをご確認ください。"
    if "city.imabari.ehime.jp" not in url:
        return simplify_title_rule_based(official_title), "（外部サイトの情報です）詳細はリンク先をご確認ください。"

    try:
        soup = fetch(url)
    except Exception as e:  # noqa: BLE001
        return simplify_title_rule_based(official_title), f"（本文の取得に失敗しました: {e}）"

    main = soup.find(id="main_container") or soup
    lead_text = extract_lead_text(main)

    # 1. GitHub Modelsが使えれば、タイトル・要約をまとめて言い換え
    ai_result = rewrite_with_github_models(official_title, lead_text)
    if ai_result:
        return ai_result

    # 2. フォールバック：ルールベースの言い換え
    plain_title = simplify_title_rule_based(official_title)
    structured = extract_structured_summary(main)
    if structured:
        summary = structured
    elif lead_text:
        summary = lead_text
    else:
        summary = "本文の要約を作成できませんでした。詳細はリンク先をご確認ください。"
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "…"
    return plain_title, summary


def main():
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=DAYS_TO_KEEP - 1)

    if GITHUB_TOKEN:
        print("[INFO] GITHUB_TOKEN を検出。GitHub Modelsでの言い換えを試みます。", file=sys.stderr)
    else:
        print("[INFO] GITHUB_TOKEN 未設定。ルールベースの言い換えのみ使用します。", file=sys.stderr)

    print(f"[INFO] fetching {BASE_URL}", file=sys.stderr)
    soup = fetch(BASE_URL)
    all_items = parse_whatsnew(soup)
    print(f"[INFO] parsed {len(all_items)} items total", file=sys.stderr)

    recent = [it for it in all_items if datetime.date.fromisoformat(it["date"]) >= cutoff]
    print(f"[INFO] {len(recent)} items within last {DAYS_TO_KEEP} days", file=sys.stderr)

    results = []
    for it in recent:
        print(f"[INFO] summarizing: {it['title'][:40]}", file=sys.stderr)
        plain_title, summary = rewrite_detail_page(it["url"], it["title"])
        results.append({
            "date": it["date"],
            "category": guess_category(it["title"]),
            "title": plain_title,
            "official_title": it["title"],
            "summary": summary,
            "url": it["url"],
        })
        time.sleep(REQUEST_DELAY_SEC)

    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": BASE_URL,
        "items": results,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[INFO] wrote {len(results)} items to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
