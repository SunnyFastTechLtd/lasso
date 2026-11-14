## What this changes

<!-- One or two sentences. Explain the problem, not just the diff. -->

## Related issue

Closes #

## How it was verified

<!-- Commands you ran, pages you clicked, or tests you added. -->

- [ ] `python manage.py test backend`
- [ ] `python manage.py makemigrations --check` (if models changed)
- [ ] `cd frontend && npm run lint && npm run build`
- [ ] Manually exercised the affected flow in the browser (for UI changes)

## Checklist

- [ ] No secrets, tokens, internal hostnames or customer data added
- [ ] User-facing text stays in Chinese, consistent with the product
- [ ] Docs updated when behaviour or configuration changed
- [ ] Commit(s) are scoped to this change
