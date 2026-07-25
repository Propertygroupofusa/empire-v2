/**
 * TypeScript types for authentication system
 */

export interface User {
  id: number;
  email: string;
  name: string;
  is_active: boolean;
  is_verified: boolean;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface RefreshTokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface ApiErrorResponse {
  detail: string;
}

export interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
}

export interface LoginCredentials {
  email: string;
  password: string;
}

export interface RegisterCredentials extends LoginCredentials {
  name: string;
}

export interface VerifyEmailRequest {
  email: string;
  code: string;
}

export interface UpdateProfileRequest {
  name: string;
}

export type AlertType = 'success' | 'error' | 'info';

export interface AlertMessage {
  message: string;
  type: AlertType;
  tabName: string;
}
