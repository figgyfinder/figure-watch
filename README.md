# Figure Watch

Checks **Mercari, Yahoo Auctions, Suruga-ya and Rakuten** every 30 minutes for
the figures in `figures.yaml`, and pings your Discord when something new is
listed, comes back in stock, or drops in price. Runs free on GitHub, so your
computer doesn't need to be on.

---

## Setup (about 15 minutes, one time)

### 1. Make a GitHub account
Sign up at https://github.com if you don't have one. The free plan is all you need.

### 2. Create the repository
1. Click **+** (top right) → **New repository**.
2. Name: `figure-watch`
3. Choose **Public**. Public repos get unlimited free run time. Your Discord
   link stays hidden either way, because it's stored as a secret.
4. Leave everything else unticked and click **Create repository**.

### 3. Upload the files
1. Unzip `figure-watch.zip` on your computer.
2. On the new repo page, click the link **"uploading an existing file"**.
3. Open the unzipped `figure-watch` folder and select **everything inside it**.
   - **Mac:** the `.github` folder is hidden. Press **Cmd + Shift + .** in Finder
     to show it, then **Cmd + A** to select all.
   - **Windows:** just **Ctrl + A**.
4. Drag the selection onto the GitHub page, wait for the upload to finish, then
   click **Commit changes**.
5. Check that the repo now shows a `.github` folder. If it doesn't, click
   **Add file → Create new file**, type `.github/workflows/check.yml` as the
   name, paste in the contents of that file from the zip, and commit.

### 4. Add your Discord webhook as a secret
1. In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `DISCORD_WEBHOOK_URL`
3. Secret: paste your webhook URL. Click **Add secret**.

### 5. Test Discord
1. Open the **Actions** tab. If GitHub asks, click the green button to enable workflows.
2. Click **Figure watch** on the left → **Run workflow** (right side).
3. Choose mode **test-discord** → **Run workflow**.
4. After about 30 seconds you should see a green tick and a ✅ message in Discord.

### 6. Do a dry run
Run it again with mode **dry-run**. Click the run → **check** → expand
**Check the sites** to see what each site returned, for example:

```
Nendoroid Miku @ Mercari: 42 results, 3 match
Nendoroid Miku @ Yahoo Auctions: 50 results, 5 match
  ! Suruga-ya failed: ...
```

If a site shows `failed`, copy those lines and send them to Claude to fix.

### 7. Switch it on
Run it once with mode **normal**. For each figure you'll get a 👀 "Now watching"
message listing what's already for sale. From then on it runs **every 30 minutes
by itself** and only pings you about **new** things.

---

## Day to day

**Add or remove figures:** open `figures.yaml` in the repo, click the ✏️
pencil, edit, then **Commit changes**. The next check picks it up. Tips:
- Search in **Japanese**. Copy the figure's name from a Japanese shop listing.
- Use `must_include` to stop unrelated results, and `exclude` to rule out
  things like `ジャンク` (junk), `箱のみ` (box only), or `ぷち` (petit).
- The **JAN** barcode is the most accurate way to search Suruga-ya and Rakuten.

**What the alerts mean:**
- **New listing:** wasn't there at the last check.
- **Back in stock:** had disappeared for 6+ hours and came back.
- **Price drop:** the same listing is now cheaper (not used for auctions).

**Pause it:** Actions → Figure watch → `···` → **Disable workflow**. Enable it the same way to start again.

**If a site stops working:** after about 3 hours of failures you'll get a ⚠️
message in Discord. The other sites keep working. Sites change their pages
now and then, so send the error to Claude and it can update the script.

---

## Optional: Rakuten

Rakuten needs free API keys. Until you add them, it's skipped.

1. Go to https://webservice.rakuten.co.jp/ and sign in with a Rakuten account
   (free to make).
2. Register a new app (アプリID発行). For the app/website URL, use your repo
   address, e.g. `https://github.com/YOUR-USERNAME/figure-watch`.
3. Add three more repository secrets (same place as step 4):
   - `RAKUTEN_APP_ID`: the アプリID / Application ID
   - `RAKUTEN_ACCESS_KEY`: the アクセスキー / Access Key (starts with `pk_`)
   - `RAKUTEN_REFERER`: the same URL you registered in step 2

---

## Good to know
- This all runs from GitHub's servers in the US, which is also why Yahoo
  Auctions works (it blocks the UK and EU).
- GitHub's schedule can run 5–15 minutes late when it's busy. That's normal.
- `state/seen.json` is the bot's memory of what it's already shown you. It
  updates itself, so don't edit it. Deleting its contents (leaving `{}`) makes
  it start fresh with new "Now watching" messages.
