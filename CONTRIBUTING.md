# Contributing to HomelabSage

Thanks for your interest. The project is pre-alpha and small, so the bar
to landing a PR is low — but a few ground rules keep the codebase coherent
and the licensing options open.

## Developer Certificate of Origin (DCO)

Every commit must be signed off with the
[Developer Certificate of Origin 1.1](https://developercertificate.org/).
This is a lightweight alternative to a CLA: it doesn't transfer copyright,
it doesn't require a separate signed document, and it doesn't need a bot
or third-party service. By signing off a commit you're stating, on the
public record, that you have the right to contribute the code under the
project's licence (AGPL-3.0 with the Plugin Exception, see
[LICENSE](LICENSE)).

The DCO text is reproduced in full at [`DCO`](DCO) and below for
convenience.

### How to sign off

Add a `Signed-off-by: Your Name <you@example.com>` line to your commit
message. The easy way is to let git add it automatically:

```
git commit -s -m "your message here"
```

Or, if you forgot, amend the last commit:

```
git commit --amend -s --no-edit
```

For a chain of commits already pushed:

```
git rebase -i HEAD~<N> --signoff
```

The sign-off identity must match your `user.name` and `user.email`. CI
will reject any commit without a sign-off.

### What signing off means

By adding the line, you certify that *one of the following is true*:

> (a) The contribution was created in whole or in part by me and I have
>     the right to submit it under the open source license indicated in
>     the file; or
> (b) The contribution is based upon previous work that, to the best of
>     my knowledge, is covered under an appropriate open source license
>     and I have the right under that license to submit that work with
>     modifications, whether created in whole or in part by me, under
>     the same open source license (unless I am permitted to submit
>     under a different license), as indicated in the file; or
> (c) The contribution was provided directly to me by some other person
>     who certified (a), (b) or (c) and I have not modified it.
> (d) I understand and agree that this project and the contribution are
>     public and that a record of the contribution (including all personal
>     information I submit with it, including my sign-off) is maintained
>     indefinitely and may be redistributed consistent with this project
>     or the open source license(s) involved.

The full text is in [`DCO`](DCO).

### Why we use a DCO instead of a CLA

A CLA (Contributor License Agreement, Apache-style) is heavier paperwork
that *transfers* certain rights from contributor to project. We don't need
that today: every contributor keeps copyright on their work, and the
project remains AGPL-licensed for everyone. If a future commercial
licensing arrangement makes sense (e.g. a paid SaaS variant), the
copyright holder can still negotiate it for their own contributions plus
any code submitted under a future amendment. DCO is the lowest-friction
way to preserve that option while keeping contribution barriers near
zero.

## Code rules

These keep the codebase honest — they're the same rules the existing
contributors follow:

1. **Tests with every behaviour change.** `pytest -q` should be green.
   Aim for one focused test per discrete behaviour, not "test_all".
2. **Lint clean.** `ruff check .` must pass. Auto-fix is OK
   (`ruff check --fix .`).
3. **No invented features.** If the changelog you're parsing doesn't say
   it, don't make the LLM say it either. The honesty rules in
   `engine.py`'s prompt template are load-bearing; don't relax them
   without discussion.
4. **One topic per PR.** A new plugin, a new detector, a doc fix — each
   gets its own PR.
5. **Plugin author?** Read the Plugin Exception in [LICENSE](LICENSE).
   You can ship your plugin under any licence you want; you only need
   AGPL compliance if you modify HomelabSage's own source.

## Reporting issues

Use GitHub Issues. For bugs, run
`homelabsage export --redact -o report.json` and attach the output — it
strips IPs, hostnames and credentials so you can paste it in a public
ticket without leaking anything from your homelab.
