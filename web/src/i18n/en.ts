// English catalog (default). No UI string is hardcoded in components
// (project i18n rule); backend error `detail` is already localized by
// the API via Accept-Language. Catalog is the shared key/shape type
// (string values, not literals) so other locales must mirror it.
export const en = {
  app: { title: 'Flow' },
  nav: { logout: 'Sign out', language: 'Language' },
  login: {
    title: 'Create account',
    email: 'Email',
    password: 'Password',
    orgName: 'Organization name',
    submit: 'Sign up',
    submitting: 'Signing up...',
    hint: 'Returning-account sign-in with multi-org selection arrives with the Tasks phase (W1).',
  },
  home: {
    title: 'My organization',
    id: 'Org ID',
    name: 'Name',
    version: 'Version',
    rename: 'Rename organization',
    newName: 'New name',
    save: 'Save',
    saving: 'Saving...',
    renamed: 'Saved.',
    loading: 'Loading...',
    conflict: 'The organization changed meanwhile; reloaded the current version.',
  },
  error: { generic: 'Something went wrong.', network: 'Network error.' },
}

export type Catalog = typeof en
