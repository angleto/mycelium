import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { RequireAuth } from './components/RequireAuth'
import { AppShell } from './components/AppShell'
import { LoginRoute } from './routes/LoginRoute'
import { RegisterRoute } from './routes/RegisterRoute'
import { VerifyEmailRoute } from './routes/VerifyEmailRoute'
import { ForgotPasswordRoute } from './routes/ForgotPasswordRoute'
import { ResetPasswordRoute } from './routes/ResetPasswordRoute'
import { HomeRoute } from './routes/HomeRoute'
import { TasksRoute } from './routes/TasksRoute'
import { TaskDetailRoute } from './routes/TaskDetailRoute'
import { WorkflowsRoute } from './routes/WorkflowsRoute'
import { GraphRoute } from './routes/GraphRoute'
import { SchedulerRoute } from './routes/SchedulerRoute'
import { EventsRoute } from './routes/EventsRoute'
import { TimeRoute } from './routes/TimeRoute'
import { AdvisoryRoute } from './routes/AdvisoryRoute'
import { BudgetsRoute } from './routes/BudgetsRoute'
import { EmailRoute } from './routes/EmailRoute'
import { SettingsRoute } from './routes/SettingsRoute'

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginRoute />} />
        <Route path="/register" element={<RegisterRoute />} />
        <Route path="/verify-email" element={<VerifyEmailRoute />} />
        <Route path="/forgot-password" element={<ForgotPasswordRoute />} />
        <Route path="/reset-password" element={<ResetPasswordRoute />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route path="/" element={<HomeRoute />} />
            <Route path="/tasks" element={<TasksRoute />} />
            <Route path="/tasks/:id" element={<TaskDetailRoute />} />
            <Route path="/workflows" element={<WorkflowsRoute />} />
            <Route path="/graph" element={<GraphRoute />} />
            <Route path="/schedule" element={<SchedulerRoute />} />
            <Route path="/calendar" element={<EventsRoute />} />
            <Route path="/time" element={<TimeRoute />} />
            <Route path="/advisory" element={<AdvisoryRoute />} />
            <Route path="/budgets" element={<BudgetsRoute />} />
            <Route path="/email" element={<EmailRoute />} />
            <Route path="/settings" element={<SettingsRoute />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
