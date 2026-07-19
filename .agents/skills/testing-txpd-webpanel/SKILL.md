---
name: testing-txpd-webpanel
description: How to end-to-end test the txpd plugin Web panel (Tencent channel management) inside ElainaBot_v2, including real-post testing and cleanup via tencent-channel-cli.
---

# Testing the txpd Web panel

## Environment
- txpd repo is symlinked into `~/ElainaBot_v2/plugins/txpd`. Start the host: `cd ~/ElainaBot_v2 && python3 main.py` (log `/tmp/elaina.log`).
- Panel: `http://127.0.0.1:5200/web/` (password `admin`). The txpd page is the sidebar item 「腾讯频道」, an iframe loading `http://127.0.0.1:5200/web/custom/txpd-panel` — you can open that URL directly.
- Panel HTML is `txpd/web/page.html`, re-read on every request: just refresh the browser after edits.
- All channel operations go through `tencent-channel-cli` (must be QR-logged-in). Each CLI call takes 3–10 s; after clicking a button, wait up to ~10 s before judging "no reaction". Success/error toasts disappear after a few seconds — screenshot within 1–3 s of clicking to catch them.

## Safety (real account!)
- Only channels explicitly whitelisted by the user (e.g. 「星星机器人」 guild `14413111660977050`, forum channel 「全部」 `635785591`) may receive real posts. Everywhere else: read-only, always click 取消 in confirm modals.

## Known pitfalls
- Chinese input via synthetic typing gets corrupted. Use clipboard instead: `sudo apt-get install -y xclip`, then `printf '中文内容' | xclip -selection clipboard` and Ctrl+A/Ctrl+V in the field.
- Markdown posts with images require `[(0,0)](@img)` placeholders in the content, otherwise the CLI rejects with a validation error.
- Image URLs may be treated as local file paths by the CLI ("文件不存在: https:/..."). Workaround: `curl -o /tmp/img.png <url>` and use the local path.
- The feed list (`feed get-guild-feeds`) items lack `channel_id`. Since PR #4 (`fillFeedParams` in page.html) the panel's 删除/点赞/评论 buttons auto-fill it via feed-detail, so UI delete works (verified). On older code they fail with 「缺少参数: channel_id」; fallback cleanup via CLI: fetch `channel_id` and `create_time_raw` via `tencent-channel-cli feed get-feed-detail --feed-id <fid> --guild-id <gid>`, then `tencent-channel-cli feed del-feed --feed-id <fid> --guild-id <gid> --channel-id <cid> --create-time <ct> --yes`.
- A saved enabled cron schedule (e.g. `*/2 * * * *`) fires on its own while you test 立即执行 — expect extra posts; delete the schedule promptly and clean up all posts it made.

## Devin Secrets Needed
- None beyond the pre-logged-in tencent-channel-cli session and panel password `admin`.
