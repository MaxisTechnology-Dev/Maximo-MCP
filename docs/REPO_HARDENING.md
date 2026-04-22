# Repository Hardening Checklist

GitHub-side controls are not visible from the working tree. This file is the
authoritative checklist for the GitHub settings that must be in place before
this repository can be used for production deployments or made public.

Treat every box below as a required step — if any item is unchecked, do not
publish the repo and do not point a production deployment at this image.

---

## Visibility & access

- [ ] Repository visibility decision recorded here:
      `<private | internal | public>` — `<reviewer @handle>` — `<date>`
- [ ] Visibility is **private** until G1, G2, G6 (per-request identity, audit
      wiring, OIDC) are merged AND a public-listing review is signed off.
- [ ] Org-wide: 2FA enforced for every collaborator with write access.
- [ ] Outside collaborators: only added with explicit reviewer approval; no
      blanket "everyone in the org" write access.

## Branch protection (`main`)

- [ ] Pull request review required (≥ 1 approving review).
- [ ] Status checks required: `ci`, `Security / gitleaks`,
      `Security / pip-audit`, `Security / trivy`, `CodeQL`.
- [ ] Conversation resolution required.
- [ ] Force push disabled (no `--force` to `main`).
- [ ] Direct push disabled (changes only via PR).
- [ ] Linear history required.
- [ ] Restrict who can dismiss reviews to the security/ops team.

## Code-scanning & supply chain

- [ ] **Secret scanning** enabled (Settings → Code security).
- [ ] **Push protection** enabled (blocks pushes that contain known secret
      patterns).
- [ ] **Dependabot alerts** enabled.
- [ ] **Dependabot security updates** enabled.
- [ ] **Dependabot version updates** configured via
      [`.github/dependabot.yml`](../.github/dependabot.yml).
- [ ] **CodeQL** workflow present
      ([`.github/workflows/codeql.yml`](../.github/workflows/codeql.yml)) and
      has run cleanly at least once.
- [ ] **Security workflow** present and green
      ([`.github/workflows/security.yml`](../.github/workflows/security.yml)):
      gitleaks + pip-audit + trivy.

## Secrets & tokens

- [ ] No long-lived `MAXIMO_*` credentials stored as repo secrets — all
      production secrets live in AWS Secrets Manager / Azure Key Vault.
- [ ] CI secrets (if any) scoped to the minimum repos and rotated quarterly.
- [ ] No secret has the same value in dev, staging, and prod.

## Tags & releases

- [ ] Releases are tagged from `main` only.
- [ ] Tag-protection rules prevent rewriting tags after release.
- [ ] Release artefacts (Docker images, etc.) signed with cosign or an
      equivalent attestation tool. (Optional but recommended.)

## Pre-publication audit

Before flipping visibility to public:

- [ ] `git ls-files | grep -E '^\.env$|\.mcp\.json$|audit\.jsonl$'` returns
      nothing.
- [ ] No tenant- or customer-specific identifiers anywhere in source,
      docstrings, or test fixtures.
- [ ] [PRODUCT_GAPS_BEFORE_DEPLOY.md](../PRODUCT_GAPS_BEFORE_DEPLOY.md) shows
      G1–G18 all marked ✅ FIXED, or each remaining gap has an explicit
      "deferred — risk owner @handle, due `<date>`" note.
- [ ] [SECURITY.md](../SECURITY.md) up to date and matches actual code
      posture.
- [ ] [README.md](../README.md) "Responsible Use" section present and
      accurate.

---

Owner: security / platform team. Review every quarter and after any
major change to the auth or audit subsystems.
