# GitHub Actions Daily Literature Digest Setup

This repository can run the daily literature digest in GitHub Actions, so it does
not depend on the Windows computer being powered on. The search configuration is
loaded from GitHub Secrets.

## What The Workflow Does

- Runs every day at 5:30 AM America/New_York.
- Searches public literature metadata sources using the scope stored in
  `DIGEST_CONFIG_JSON`.
- Uses the OpenAI API by default to write the English-only email digest.
- Can optionally use Anthropic Claude by setting `MODEL_PROVIDER=anthropic`.
- Sends the digest through Gmail SMTP using the sender and recipient stored in
  GitHub Secrets.
- Does not upload digest artifacts or commit sent-item history.
- Uses an Actions cache with compact sent-item fingerprints to reduce repeat
  entries across runs.
- Sends all discovered candidate records that fit the digest configuration, up
  to the global candidate cap. The model formats and summarizes the digest; it
  is not asked to choose only a small subset.

## Required GitHub Secrets

In the GitHub repository, go to:

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Add these secrets:

```text
OPENAI_API_KEY
DIGEST_CONFIG_JSON
GMAIL_ADDRESS
GMAIL_APP_PASSWORD
DIGEST_RECIPIENT
```

Recommended values:

```text
GMAIL_ADDRESS=<your Gmail address>
DIGEST_RECIPIENT=<your recipient email address>
```

`GMAIL_APP_PASSWORD` should be a fresh Gmail App Password for the Gmail account
stored in `GMAIL_ADDRESS`. Do not use the normal Gmail login password.

`DIGEST_CONFIG_JSON` should contain the full digest configuration. Keep that JSON
out of Git and paste it into the GitHub Secret. A local ignored file named
`DIGEST_CONFIG_JSON` can be used for editing before you copy it to GitHub.

If you use the GitHub CLI, you can set the config secret from PowerShell:

```powershell
Get-Content .\DIGEST_CONFIG_JSON -Raw | gh secret set DIGEST_CONFIG_JSON
```

Or paste the full file contents into the GitHub web UI when creating the
`DIGEST_CONFIG_JSON` repository secret.

Optional Claude secret:

```text
ANTHROPIC_API_KEY
```

You only need this if you set `MODEL_PROVIDER=anthropic`.

## Optional GitHub Variables

You may also add a repository variable:

```text
MODEL_PROVIDER=openai
OPENAI_MODEL=gpt-5-mini
```

If these variables are omitted, the script uses OpenAI with `gpt-5-mini`.

To test Claude instead, add:

```text
MODEL_PROVIDER=anthropic
ANTHROPIC_MODEL=claude-haiku-4-5
ANTHROPIC_VERSION=2023-06-01
```

Optional variables:

```text
MAX_CANDIDATES_FOR_MODEL=80
MAX_EMAIL_CANDIDATES=60
SECTION_A_CANDIDATE_CAP=20
SECTION_B_CANDIDATE_CAP=15
SECTION_C_CANDIDATE_CAP=40
RESPECT_SECTION_ROW_LIMITS=false
MAX_OUTPUT_TOKENS=9000
OPENAI_MAX_OUTPUT_TOKENS=9000
ANTHROPIC_MAX_OUTPUT_TOKENS=9000
```

By default, per-section row limits in the digest config are ignored and the
workflow uses the global candidate cap instead. Set
`RESPECT_SECTION_ROW_LIMITS=true` only if you want the digest config's
`rows_per_query` or `rows_per_journal` values to act as hard search limits.
The email-length caps above are defaults; override them with repository
variables only if you want longer or shorter digests.

Section configs may also include:

```json
"exclude_terms": ["term one", "term two"]
```

Any candidate whose title, venue, abstract, author field, or match note contains
one of those terms will be removed before the email is written.

## First Test

After pushing the files to GitHub:

1. Open the repository on GitHub.
2. Go to `Actions`.
3. Select `Daily Literature Digest`.
4. Click `Run workflow`.

Manual runs skip the 5:30 AM time gate. Scheduled runs use the time gate so the
two UTC cron entries do not send duplicate emails across daylight-saving changes.

## Important Notes

- GitHub Actions schedule times are not guaranteed to start at the exact minute;
  a small delay is normal.
- A small Actions cache is used to reduce repeat entries across runs. Cache
  entries may expire, so occasional repeats can still happen.
- To change the delivery address later, update only the `DIGEST_RECIPIENT`
  secret.
