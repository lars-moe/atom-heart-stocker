# Run Atom Heart Stocker on GitHub Actions (free, no computer needed)

GitHub runs the trading sessions on its own servers on weekdays. The portfolio state is saved
in a branch called `state`, and the dashboard is published as a web page you can open
from any device.

**About 10 minutes, once.**

## 1. Create the repository
1. Sign in at github.com (or create a free account).
2. Click **+ → New repository**.
3. Name it `atom-heart-stocker`, choose **Public**, leave "Add a README" unticked, then click **Create repository**.

A public repository is what makes it free, including the dashboard page. Your code and paper portfolio
will be visible to anyone with the link. Never upload a `.env` file: `.gitignore` already blocks it.

## 2. Upload the code
**Option A, with git** (in a terminal, inside the unzipped `atom_heart_stocker` folder):
```bash
git init -b main
git add .
git commit -m "Atom Heart Stocker"
git remote add origin https://github.com/YOUR-USERNAME/atom-heart-stocker.git
git push -u origin main
```

**Option B, in the browser:** on the new repository page, click **uploading an existing file**, drag in
**all contents** of the folder, then click **Commit changes**.
Make sure the hidden `.github` folder is included. On a Mac, press **Cmd + Shift + .** in Finder to show hidden files first.
Afterwards, check that the repository shows `.github/workflows/trade.yml`.

## 3. Turn on the dashboard page
**Settings → Pages → Build and deployment → Source: GitHub Actions.**

## 4. (Optional) Let Claude review buys and write the daily note
**Settings → Secrets and variables → Actions → New repository secret**:
name `ANTHROPIC_API_KEY`, value = your key. Without it, everything else still works.

## 5. Test it
1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Choose **trade → Run workflow**, set session = `nordic` and tick **dry_run**, then click **Run**.
   The run takes a few minutes the first time, because it downloads ~10 years of prices.
3. When it turns green, the dashboard is at
   `https://YOUR-USERNAME.github.io/atom-heart-stocker/`.
   The research tab shows what the team *would* have done.

Don't use a manual run without dry_run outside market hours: it forces trades at stale prices.

## 6. That's it
From now on the `trade` workflow fires on weekday mornings and afternoons and runs:

| Session | Oslo time |
|---|---|
| Nordic | ~10:40 |
| Europe | ~10:40 |
| US | ~16:50 (15:50 for a few weeks in March and Oct/Nov) |

Each session runs once per day. Extra triggers later in the day catch up if GitHub delayed or dropped a run,
and a failed session (for example, Yahoo rate-limiting) is retried up to 3 times while the market is open.
Holidays are skipped automatically.

| Where | What it shows |
|---|---|
| Actions tab | every run and its log (what was bought or sold, and why) |
| `state` branch | the saved portfolios (`state/ledger.sqlite`) |
| Dashboard page | updates after each session |

## If something goes wrong
- **Run fails at "Save portfolio state"**: go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and save.
- **Run fails at "Publish dashboard"**: make sure step 3 was done (Source: GitHub Actions).
- **"No data" for many stocks / rate limited**: Yahoo sometimes blocks GitHub's servers. The run retries
  automatically. If it keeps happening, switch the price source to a paid service (for example EODHD, about €20/month).
  Only `ahs/sources.py` needs changing.
- **Scheduled runs stopped**: GitHub pauses schedules in repositories that have had no activity for 60 days. The `keepalive`
  workflow re-enables it twice a month. You can also click **Enable workflow** in the Actions tab.
- **Start the portfolios over**: delete the `state` branch (**Branches → state → delete**).
