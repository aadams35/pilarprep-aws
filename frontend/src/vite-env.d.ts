interface ImportMetaEnv {
  readonly VITE_PILLARPREP_JOBS_API_URL?: string;
  readonly VITE_PILLARPREP_WORKSPACE_API_URL?: string;
  readonly VITE_PILLARPREP_BACKEND_REGION?: string;
  readonly VITE_PILLARPREP_COGNITO_IDENTITY_POOL_ID?: string;
  readonly VITE_PILLARPREP_COGNITO_USER_POOL_CLIENT_ID?: string;
  readonly VITE_PILLARPREP_COGNITO_LOGIN_DOMAIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
