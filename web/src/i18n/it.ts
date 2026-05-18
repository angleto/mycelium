import type { Catalog } from './en'

// Italian catalog. Same key shape as en (typed via Catalog).
export const it: Catalog = {
  app: { title: 'Flow' },
  nav: { logout: 'Esci', language: 'Lingua' },
  login: {
    title: 'Crea account',
    email: 'Email',
    password: 'Password',
    orgName: "Nome dell'organizzazione",
    submit: 'Registrati',
    submitting: 'Registrazione...',
    hint: "L'accesso con account esistente e selezione multi-organizzazione arriva con la fase Task (W1).",
  },
  home: {
    title: 'La mia organizzazione',
    id: 'ID org',
    name: 'Nome',
    version: 'Versione',
    rename: "Rinomina l'organizzazione",
    newName: 'Nuovo nome',
    save: 'Salva',
    saving: 'Salvataggio...',
    renamed: 'Salvato.',
    loading: 'Caricamento...',
    conflict: "L'organizzazione e cambiata nel frattempo; ricaricata la versione corrente.",
  },
  error: { generic: 'Si e verificato un errore.', network: 'Errore di rete.' },
}
