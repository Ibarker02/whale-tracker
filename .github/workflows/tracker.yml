name: Polymarket Whale Tracker

on:
  schedule:
    - cron: "*/5 * * * *"   # Every 5 minutes
  workflow_dispatch: {}   # adds a "Run workflow" button for manual runs

permissions:
  contents: write

# Never let two runs overlap (they would fight over the state file).
concurrency:
  group: whale-tracker
  cancel-in-progress: false

jobs:
  track:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Check out the repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # The tracker's memory is stored ENCRYPTED (state.json.enc) so nobody
      # can read your wallets from this public repo. Never commit state.json.
      - name: Decrypt state
        env:
          STATE_PASSWORD: ${{ secrets.STATE_PASSWORD }}
        run: |
          if [ -z "$STATE_PASSWORD" ]; then
            echo "::error::Add a STATE_PASSWORD secret first."; exit 1
          fi
          rm -f state.json
          if [ -f state.json.enc ]; then
            openssl enc -aes-256-cbc -d -pbkdf2 -in state.json.enc -out state.json -pass env:STATE_PASSWORD
          fi

      - name: Run the tracker
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          WALLETS: ${{ secrets.WALLETS }}
          OFFICIAL_WEBHOOK_URL: ${{ secrets.OFFICIAL_WEBHOOK_URL }}
          DEFAULT_MIN_TRADE_USD: ${{ vars.DEFAULT_MIN_TRADE_USD }}
        run: python tracker.py

      # Save progress even if the tracker step failed - but only the
      # encrypted copy, and make sure no plain state.json is ever committed.
      - name: Encrypt and save state
        if: always()
        env:
          STATE_PASSWORD: ${{ secrets.STATE_PASSWORD }}
        run: |
          if [ ! -f state.json ] || [ -z "$STATE_PASSWORD" ]; then exit 0; fi
          openssl enc -aes-256-cbc -salt -pbkdf2 -in state.json -out state.json.enc -pass env:STATE_PASSWORD
          rm -f state.json
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git rm -q --cached --ignore-unmatch state.json
          git add state.json.enc
          if git diff --cached --quiet; then
            echo "State unchanged - nothing to save."
          else
            git commit -m "Update encrypted tracker state"
            git push
          fi
