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
- NumPy 2.4.6, BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0
- SciPy 1.16.2, BSD-style three-clause license; accompanying binary distribution
  notices include OpenBLAS, LAPACK, GCC runtime (GPL with GCC runtime exception),
  and libquadmath (LGPL). The complete supplied notices are retained in
  [SciPy license text](docs/licenses/scipy-1.16.2.txt).
- HiGHS, MIT, as pinned by the SciPy source distribution at
  `222cce79a2bca866dbfbcd91b55da11336ae88f4`; see its
  [license text](docs/licenses/highs.txt) and the
  [upstream source](https://github.com/scipy/HiGHs/tree/222cce79a2bca866dbfbcd91b55da11336ae88f4).
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
The supplied NumPy notice is also retained in
[NumPy license text](docs/licenses/numpy-2.4.6.txt). Binary dependencies remain
separate upstream distributions; this package does not vendor or modify them.
