import { lazy, Suspense } from "react";
import { createBrowserRouter } from "react-router-dom";
import AppShell from "./components/AppShell";
import { NETHEALTH_ENABLED, UNIVERSAL_ENABLED } from "./lib/featureFlags";

const DashboardPage = lazy(() => import("./pages/Dashboard"));
const IntelFeedPage = lazy(() => import("./pages/IntelFeed"));
const ResponseOpsPage = lazy(() => import("./pages/ResponseOps"));
const NotFoundPage = lazy(() => import("./pages/NotFound"));
const AuthCallbackPage = lazy(() => import("./pages/AuthCallback"));
const ProtectionPage = lazy(() => import("./pages/Protection"));
const NetworkHealthPage = lazy(() => import("./pages/NetworkHealth"));
const CustomerDemoPage = lazy(() => import("./pages/CustomerDemo"));

function LazyPage({ children }) {
  return (
    <Suspense fallback={<div className="py-16 text-center text-sm text-soc-muted">Loading…</div>}>
      {children}
    </Suspense>
  );
}

const router = createBrowserRouter([
  {
    path: "/callback",
    element: (
      <LazyPage>
        <AuthCallbackPage />
      </LazyPage>
    ),
  },
  {
    path: "/demo",
    element: (
      <LazyPage>
        <CustomerDemoPage />
      </LazyPage>
    ),
  },
  {
    path: "/",
    element: <AppShell />,
    children: [
      {
        index: true,
        element: (
          <LazyPage>
            <DashboardPage />
          </LazyPage>
        ),
      },
      {
        path: "intel-feed",
        element: (
          <LazyPage>
            <IntelFeedPage />
          </LazyPage>
        ),
      },
      {
        path: "response-ops",
        element: (
          <LazyPage>
            <ResponseOpsPage />
          </LazyPage>
        ),
      },
      ...(UNIVERSAL_ENABLED
        ? [{
            path: "protection",
            element: (
              <LazyPage>
                <ProtectionPage />
              </LazyPage>
            ),
          }]
        : []),
      ...(NETHEALTH_ENABLED
        ? [{
            path: "network-health",
            element: (
              <LazyPage>
                <NetworkHealthPage />
              </LazyPage>
            ),
          }]
        : []),
      {
        path: "*",
        element: (
          <LazyPage>
            <NotFoundPage />
          </LazyPage>
        ),
      },
    ],
  },
]);

export default router;
