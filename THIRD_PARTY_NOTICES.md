# Third-Party Notices

This engineering baseline depends on the following direct runtime and Web
components:

- M-Agent 0.5.1, Apache-2.0
- FastAPI, MIT
- SQLAlchemy, MIT
- Alembic, MIT
- Pydantic and pydantic-settings, MIT
- structlog, Apache-2.0 or MIT
- Typer, MIT
- Uvicorn, BSD-3-Clause
- webauthn, BSD-3-Clause
- @hookform/resolvers, MIT
- @radix-ui/react-slot, MIT
- @simplewebauthn/browser, MIT
- @tanstack/react-query, MIT
- @tailwindcss/vite and Tailwind CSS, MIT
- @vitejs/plugin-react, MIT
- lucide-react, ISC
- openapi-fetch, MIT
- react and react-dom (React), MIT
- react-hook-form (React Hook Form), MIT
- react-router (React Router), MIT
- vite and vite-plugin-pwa (Vite), MIT
- zod (Zod), MIT
- TypeScript, Apache-2.0

The complete resolved dependency identities are recorded in `uv.lock` and
`web/pnpm-lock.yaml`. `make license-check` verifies direct Python component
metadata and every pnpm-resolved license against the public baseline policy.
Upstream license texts and notices remain governed by the applicable upstream
distributions.
