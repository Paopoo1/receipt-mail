# 領収書メール

毎月の出張（第 1 土曜〜月曜）のタクシー・新幹線・マリンライナー（高松→岡山）の領収書を
スマホで撮って、決まった宛先にまとめてメールするアプリ。Railway の 1 サービスで動く。

## 使い方
1. アプリを開くと今月の出張日程（例 10/3(土)〜10/5(月)）が出る
2. 種類ごとに「📷 撮影」→ 日付・金額（任意）を選ぶ
3. 下の「メールで送る」→ 内容を確認して「送信する」
4. 通知を有効にしておくと、出張初日の朝 8 時と、月曜 19 時（未送信なら 1 週間毎日）に知らせる

## Railway の Variables
| 名前 | 内容 |
|---|---|
| `APP_PASSWORD` | アプリのログインパスワード（必須） |
| `DATA_DIR` | `/data`（ボリュームを /data にマウント） |
| `SMTP_USER` / `SMTP_PASS` | 送信元 Gmail と アプリパスワード（16 桁） |
| `SMTP_HOST` / `SMTP_PORT` | 省略時 `smtp.gmail.com` / `465` |
| `MAIL_TO` / `MAIL_CC` / `SENDER_NAME` | 既定の宛先など（アプリの ⚙ で上書きできる） |
| `RESEND_API_KEY` / `MAIL_FROM` | SMTP の代わりに Resend を使う場合 |

送信設定が無いときは `DATA_DIR/outbox/` に .eml を保存するだけ（テスト用）。

ローカル：`pip install -r requirements.txt && python server.py` → http://localhost:8000
