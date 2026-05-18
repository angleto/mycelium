// English catalog (default). No UI string is hardcoded in components
// (project i18n rule); backend error `detail` is already localized by
// the API via Accept-Language. Catalog is the shared key/shape type
// (string values, not literals) so other locales must mirror it.
export const en = {
  app: { title: 'Flow' },
  nav: { logout: 'Sign out', language: 'Language', settings: 'Settings', home: 'Home' },
  auth: {
    email: 'Email',
    password: 'Password',
    displayName: 'Display name (optional)',
    signIn: 'Sign in',
    signUp: 'Create account',
    working: 'Please wait...',
    toRegister: 'Create an account',
    toLogin: 'Back to sign in',
    forgotLink: 'Forgot password?',
  },
  login: {
    title: 'Sign in',
    mfaTitle: 'Two-factor code',
    mfaCode: 'Authentication code',
    verify: 'Verify',
    emailNotVerified: 'Your email is not verified yet.',
    resend: 'Resend verification email',
    resent: 'Verification email sent if the address exists.',
    locked: 'Account temporarily locked after repeated failures. Try again later.',
  },
  register: {
    title: 'Create your account',
    hint: 'A personal workspace is created for you automatically.',
    checkEmail: 'Check your email to confirm your address, then sign in.',
  },
  verify: {
    title: 'Email verification',
    working: 'Verifying...',
    failed: 'This verification link is invalid or expired.',
  },
  forgot: {
    title: 'Reset password',
    submit: 'Send reset link',
    done: 'If that address exists, a reset link has been sent.',
  },
  reset: {
    title: 'Choose a new password',
    newPassword: 'New password',
    submit: 'Set password',
    done: 'Password updated. You can sign in now.',
  },
  mfa: {
    title: 'Two-factor authentication',
    enabled: 'Enabled',
    disabled: 'Disabled',
    setup: 'Set up authenticator',
    scan: 'Scan this QR in your authenticator app, or enter the secret:',
    enterCode: 'Enter the 6-digit code to activate',
    activate: 'Activate',
    backupTitle: 'Backup codes (shown once, store them safely)',
    disableTitle: 'Disable two-factor',
    disableCode: 'Current code or a backup code',
    disable: 'Disable',
  },
  switcher: {
    label: 'Workspace',
    create: 'New workspace',
    newName: 'Workspace name',
    creating: 'Creating...',
  },
  home: {
    title: 'My workspace',
    id: 'Workspace ID',
    name: 'Name',
    version: 'Version',
    rename: 'Rename workspace',
    newName: 'New name',
    save: 'Save',
    saving: 'Saving...',
    renamed: 'Saved.',
    loading: 'Loading...',
    conflict: 'The workspace changed meanwhile; reloaded the current version.',
  },
  error: { generic: 'Something went wrong.', network: 'Network error.' },
}

export type Catalog = typeof en
