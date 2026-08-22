import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { FocusProvider } from './lib/focus'
import { RequireAuth } from './components/RequireAuth'
import { AppShell } from './components/AppShell'
import { LoginRoute } from './routes/LoginRoute'
import { RegisterRoute } from './routes/RegisterRoute'
import { VerifyEmailRoute } from './routes/VerifyEmailRoute'
import { ForgotPasswordRoute } from './routes/ForgotPasswordRoute'
import { ResetPasswordRoute } from './routes/ResetPasswordRoute'
import { TasksRoute } from './routes/TasksRoute'
import { AdminUsersRoute } from './routes/AdminUsersRoute'
import { AuthLayout } from './components/AuthLayout'
import { TrashRoute } from './routes/TrashRoute'
import { ClientsProjectsRoute } from './routes/ClientsProjectsRoute'
import { TaskDetailRoute } from './routes/TaskDetailRoute'
import { WorkflowsRoute } from './routes/WorkflowsRoute'
import { GraphRoute } from './routes/GraphRoute'
import { SchedulerRoute } from './routes/SchedulerRoute'
import { EventsRoute } from './routes/EventsRoute'
import { TimeRoute } from './routes/TimeRoute'
import { AdvisoryRoute } from './routes/AdvisoryRoute'
import { BudgetsRoute } from './routes/BudgetsRoute'
import { EmailRoute } from './routes/EmailRoute'
import { BillingRoute } from './routes/BillingRoute'
import { MemoryRoute } from './routes/MemoryRoute'
import { NotesRoute } from './routes/NotesRoute'
import { NoteDetailRoute } from './routes/NoteDetailRoute'
import { GardenRoute } from './routes/GardenRoute'
import { GardenHealthRoute } from './routes/GardenHealthRoute'
import { GardenAuditRoute } from './routes/GardenAuditRoute'
import { GardenReviewRoute } from './routes/GardenReviewRoute'
import { InvoicesRoute } from './routes/InvoicesRoute'
import { NotificationsRoute } from './routes/NotificationsRoute'
import { TagManagerRoute } from './routes/TagManagerRoute'
import { SettingsLayout } from './routes/SettingsLayout'
import { SettingsAccountRoute } from './routes/SettingsAccountRoute'
import { SettingsWorkspaceRoute } from './routes/SettingsWorkspaceRoute'
import { SettingsPlatformRoute } from './routes/SettingsPlatformRoute'
import { PrefixOrUuid, PrefixResolver } from './routes/PrefixResolver'

function App() {
  return (
    <BrowserRouter>
      <FocusProvider>
      <Routes>
        <Route element={<AuthLayout />}>
          <Route path="/login" element={<LoginRoute />} />
          <Route path="/register" element={<RegisterRoute />} />
          <Route path="/verify-email" element={<VerifyEmailRoute />} />
          <Route path="/forgot-password" element={<ForgotPasswordRoute />} />
          <Route path="/reset-password" element={<ResetPasswordRoute />} />
        </Route>
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route path="/" element={<Navigate to="/notes" replace />} />
            <Route path="/tasks" element={<TasksRoute />} />
            <Route
              path="/tasks/:id"
              element={
                <PrefixOrUuid kind="task">
                  <TaskDetailRoute />
                </PrefixOrUuid>
              }
            />
            <Route path="/t/:prefix" element={<PrefixResolver kind="task" />} />
            <Route path="/trash" element={<TrashRoute />} />
            <Route path="/clients" element={<ClientsProjectsRoute />} />
            <Route path="/workflows" element={<WorkflowsRoute />} />
            <Route path="/graph" element={<GraphRoute />} />
            <Route path="/schedule" element={<SchedulerRoute />} />
            <Route path="/calendar" element={<EventsRoute />} />
            <Route path="/time" element={<TimeRoute />} />
            <Route path="/advisory" element={<AdvisoryRoute />} />
            <Route path="/budgets" element={<BudgetsRoute />} />
            <Route path="/email" element={<EmailRoute />} />
            <Route path="/billing" element={<BillingRoute />} />
            <Route path="/memory" element={<MemoryRoute />} />
            <Route path="/notes" element={<NotesRoute />} />
            <Route
              path="/notes/:id"
              element={
                <PrefixOrUuid kind="note">
                  <NoteDetailRoute />
                </PrefixOrUuid>
              }
            />
            <Route path="/n/:prefix" element={<PrefixResolver kind="note" />} />
            <Route path="/garden" element={<GardenRoute />} />
            <Route path="/garden/health" element={<GardenHealthRoute />} />
            <Route path="/garden/audit" element={<GardenAuditRoute />} />
            <Route path="/garden/review" element={<GardenReviewRoute />} />
            <Route path="/invoices" element={<InvoicesRoute />} />
            <Route path="/notifications" element={<NotificationsRoute />} />
            <Route path="/tags" element={<TagManagerRoute />} />
            {/* Settings is a section, not a page: account / workspace /
                platform are three different SCOPES and the tab you are on
                is what says which one you are editing. ``/settings`` keeps
                rendering the account panel so old links and bookmarks
                still land on something. */}
            <Route path="/settings" element={<SettingsLayout />}>
              <Route index element={<SettingsAccountRoute />} />
              <Route path="workspace" element={<SettingsWorkspaceRoute />} />
              <Route path="platform" element={<SettingsPlatformRoute />} />
            </Route>
            {/* The workspace surface moved into Settings; the old
                top-level route stays as a redirect. */}
            <Route
              path="/workspace"
              element={<Navigate to="/settings/workspace" replace />}
            />
            <Route path="/admin/users" element={<AdminUsersRoute />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/notes" replace />} />
      </Routes>
      </FocusProvider>
    </BrowserRouter>
  )
}

export default App
