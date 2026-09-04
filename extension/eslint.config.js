import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', 'src/styles/tokens.generated.css']),
  {
    files: ['**/*.ts'],
    extends: [js.configs.recommended, tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      globals: { ...globals.browser, ...globals.serviceworker, chrome: 'readonly' },
    },
    rules: {
      // Omitting a property by rest destructuring is how the secret is
      // stripped before a connection row crosses the seam. The binding
      // is unused ON PURPOSE -- that is the whole mechanism.
      '@typescript-eslint/no-unused-vars': ['error', { ignoreRestSiblings: true }],
      // The barrel is the entry point on both sides of the boundary.
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@shared/*'],
              message:
                'web/src/shared/index.ts is the entry point. Import from "@shared", not past it.',
            },
          ],
        },
      ],
    },
  },
  {
    // The panel documents may not reach the network. Everything they need
    // goes through the typed operation seam, which is what makes the
    // on/off switch a guarantee (one place to gate) and the
    // loading/error/empty mechanism unavoidable rather than conventional.
    //
    // Scoped to src/ui deliberately: the tests DO import the worker's
    // modules, which is how a unit test of the network seam or of the
    // captured link is written at all.
    files: ['src/ui/**/*.ts'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['**/bg/*'],
              message:
                'UI code must not import the worker. Add an operation to shared/protocol.ts and call it with send().',
            },
            {
              group: ['@shared/*'],
              message:
                'web/src/shared/index.ts is the entry point. Import from "@shared", not past it.',
            },
          ],
        },
      ],
    },
  },
  {
    // The build scripts run in node and are typed by JSDoc.
    files: ['scripts/**/*.mjs', 'vite.config.ts'],
    extends: [js.configs.recommended],
    languageOptions: { ecmaVersion: 2023, sourceType: 'module', globals: globals.node },
  },
])
