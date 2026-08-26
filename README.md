# 斬 -サムライソード- オンライン（Webアプリ版）

Firebase Realtime Database で同期する、静的HTML1枚のオンライン対戦アプリです。
ビルド作業は不要で、`index.html` を静的ホスティングに置くだけで動きます。

---

## 1. Firebase 側の設定（初回のみ）

### Authentication
「構築 → Authentication → Sign-in method」で **匿名（Anonymous）** を **有効** にする。

### Realtime Database のルール
「構築 → Realtime Database → ルール」を以下に置き換えて「公開」。

```json
{
  "rules": {
    "zan": {
      "$room": {
        ".read": "auth != null",
        ".write": "auth != null",
        ".validate": "$room.length <= 24"
      }
    }
  }
}
```

### 承認済みドメイン
「Authentication → 設定 → 承認済みドメイン」に、公開先のドメインを追加する。
GitHub Pages の場合は `<ユーザー名>.github.io` を追加。
（`localhost` は最初から入っているので、手元での動作確認はそのまま可能）

---

## 2. GitHub Pages で公開する

このアプリは **`index.html` 単体ではなく、`images/` フォルダも一緒にアップロードする必要があります**（カードイラストが外部ファイル参照になっているため）。

1. GitHub で新しいリポジトリを作る（例：`zan-online`）
   - Public / Private どちらでも Pages は使えます（Private は有料プランの場合あり。迷ったら Public でOK）
2. リポジトリの「Add file → Upload files」を開き、**`index.html` と `images` フォルダを丸ごとドラッグ＆ドロップ**して Commit
   - 同梱の `zan-online.zip` を展開すると、`index.html` と `images/weapon` `images/kakugo` `images/action` `images/bujin` の4フォルダ（計67枚の `.webp` 画像）が出てきます。この中身をそのままアップロードしてください。
   - GitHubのアップロード画面はフォルダごとドラッグすると自動でパスを保った状態でアップロードされます。
3. 「Settings → Pages」を開く
   - **Source**: `Deploy from a branch`
   - **Branch**: `main` / `/ (root)` を選んで Save
4. 1〜2分待つと `https://<ユーザー名>.github.io/zan-online/` で公開されます

このURLを仲間に配れば、全員が同じ部屋番号を入力するだけで対戦できます。
Claudeアカウントもログインも不要です。

### フォルダ構成（アップロード後）
```
（リポジトリのルート）
├─ index.html
└─ images/
   ├─ weapon/   （武器カード 23枚 .webp）
   ├─ kakugo/   （覚悟カード 9枚 .webp）
   ├─ action/   （行動カード 12枚 .webp）
   └─ bujin/    （武人カード 23枚 .webp）
```
この階層関係が崩れるとカード画像が表示されなくなるので、`index.html` と `images` は必ず同じ階層に置いてください。

---

## 3. 遊び方（従来どおり）

1. 全員が公開URLを開く
2. 名前と部屋番号（合言葉、例：`SAKURA`）を入力して参加
3. 3〜8人揃ったら主催者が「ゲームを開始する」

切断した場合は、**同じ部屋番号・同じ名前**で入り直せば途中から復帰できます。

---

## 4. 変更点（アーティファクト版との差分）

| | アーティファクト版 | Webアプリ版 |
|---|---|---|
| 共有状態 | `window.storage`（Claude専用） | Firebase Realtime Database |
| 同期方式 | 2.5秒ポーリング | `onValue` リアルタイム購読（即時反映） |
| 自分の識別情報 | 個人ストレージ | `sessionStorage`（タブ単位） |
| 部屋の保存先 | `zan_game_<部屋番号>` | `/zan/<部屋番号>/state` |
| カード画像 | HTML内にbase64で直接埋め込み（圧縮JPEG） | `images/` フォルダの外部ファイル（WebP、高画質） |

ゲームロジック・カード効果・UIは一切変更していません。

### カード画像について
全67種（武器23・覚悟9・行動12・武人23）のイラストを、高画質のPNG原本からWebP形式に変換して同梱しています（1枚あたり数十KB程度、67枚合計で約2.3MB）。以前のbase64埋め込み版（1枚140px・quality55の圧縮JPEG）より鮮明に表示されます。

---

## 5. 運用メモ

- **APIキーの公開について**：`index.html` 内の `firebaseConfig` にはAPIキーが含まれますが、これはウェブ用の公開前提の識別子です。実際のアクセス制御は上記セキュリティルール（匿名ログイン必須）で行っています。
- **部屋データの掃除**：使い終わった部屋は Realtime Database のコンソールから `/zan/<部屋番号>` を削除できます。放置しても無料枠を圧迫する量ではありません。
- **無料枠**：Spark プラン（無料）は同時接続100・保存1GB・転送10GB/月。仲間内利用なら十分です。

---

## 6. 次にやると良いこと（任意）

- **カード画像の外部ファイル化**：現在は58枚をHTMLに埋め込んでいるため画質を落としてあります。`images/` フォルダに分離すれば、サイズ制限がなくなり高画質に戻せます（初回読み込みも速くなります）。
- **部屋の自動削除**：Cloud Functions で古い部屋を定期削除。
