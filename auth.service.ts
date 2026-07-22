/**
 * Authentication service with full TypeScript typing
 */

import {
  User,
  TokenResponse,
  RefreshTokenResponse,
  ApiErrorResponse,
  AuthState,
  LoginCredentials,
  RegisterCredentials,
  VerifyEmailRequest,
  UpdateProfileRequest,
} from './auth.types';

export class AuthService {
  private apiUrl: string;
  private authState: AuthState;

  constructor(apiUrl: string = 'http://localhost:8000') {
    this.apiUrl = apiUrl;
    this.authState = {
      accessToken: this.loadFromStorage('accessToken'),
      refreshToken: this.loadFromStorage('refreshToken'),
      user: this.loadUserFromStorage(),
    };
  }

  /**
   * Load token from localStorage
   */
  private loadFromStorage(key: string): string | null {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  /**
   * Load user object from localStorage
   */
  private loadUserFromStorage(): User | null {
    try {
      const stored = localStorage.getItem('user');
      return stored ? JSON.parse(stored) : null;
    } catch {
      return null;
    }
  }

  /**
   * Parse API error response
   */
  private async parseError(response: Response): Promise<string> {
    try {
      const error = (await response.json()) as ApiErrorResponse;
      return error.detail || 'An error occurred';
    } catch {
      return `HTTP ${response.status}: ${response.statusText}`;
    }
  }

  /**
   * Make authenticated request
   */
  private async fetchWithAuth(
    url: string,
    options: RequestInit = {}
  ): Promise<Response> {
    const headers = new Headers(options.headers || {});

    if (!headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }

    if (this.authState.accessToken) {
      headers.set('Authorization', `Bearer ${this.authState.accessToken}`);
    }

    return fetch(url, {
      ...options,
      headers,
    });
  }

  /**
   * Register a new user
   */
  async register(credentials: RegisterCredentials): Promise<TokenResponse> {
    const response = await fetch(`${this.apiUrl}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(credentials),
    });

    if (!response.ok) {
      throw new Error(await this.parseError(response));
    }

    const data = (await response.json()) as TokenResponse;
    this.setAuthState(data);
    return data;
  }

  /**
   * Login with credentials
   */
  async login(credentials: LoginCredentials): Promise<TokenResponse> {
    const response = await fetch(`${this.apiUrl}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(credentials),
    });

    if (!response.ok) {
      throw new Error(await this.parseError(response));
    }

    const data = (await response.json()) as TokenResponse;
    this.setAuthState(data);
    return data;
  }

  /**
   * Verify email with code
   */
  async verifyEmail(request: VerifyEmailRequest): Promise<{ message: string }> {
    const response = await fetch(`${this.apiUrl}/auth/verify-email`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      throw new Error(await this.parseError(response));
    }

    return (await response.json()) as { message: string };
  }

  /**
   * Refresh access token
   */
  async refreshToken(): Promise<RefreshTokenResponse> {
    if (!this.authState.refreshToken) {
      throw new Error('No refresh token available');
    }

    const response = await fetch(`${this.apiUrl}/auth/refresh-token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: this.authState.refreshToken }),
    });

    if (!response.ok) {
      throw new Error(await this.parseError(response));
    }

    const data = (await response.json()) as RefreshTokenResponse;
    this.authState.accessToken = data.access_token;
    localStorage.setItem('accessToken', data.access_token);
    return data;
  }

  /**
   * Get current user profile (protected)
   */
  async getProfile(): Promise<User> {
    const response = await this.fetchWithAuth(`${this.apiUrl}/auth/me`);

    if (!response.ok) {
      throw new Error(await this.parseError(response));
    }

    return (await response.json()) as User;
  }

  /**
   * Update user profile (protected)
   */
  async updateProfile(request: UpdateProfileRequest): Promise<User> {
    const response = await this.fetchWithAuth(`${this.apiUrl}/auth/me`, {
      method: 'PUT',
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      throw new Error(await this.parseError(response));
    }

    const updatedUser = (await response.json()) as User;
    if (this.authState.user) {
      this.authState.user = updatedUser;
      localStorage.setItem('user', JSON.stringify(updatedUser));
    }
    return updatedUser;
  }

  /**
   * Logout user (protected)
   */
  async logout(): Promise<void> {
    try {
      const response = await this.fetchWithAuth(`${this.apiUrl}/auth/logout`, {
        method: 'POST',
      });

      if (!response.ok) {
        console.error('Logout error:', await this.parseError(response));
      }
    } catch (error) {
      console.error('Logout error:', error);
    }

    this.clearAuthState();
  }

  /**
   * Set authentication state and persist to localStorage
   */
  private setAuthState(data: TokenResponse): void {
    this.authState.accessToken = data.access_token;
    this.authState.refreshToken = data.refresh_token;
    this.authState.user = data.user;

    localStorage.setItem('accessToken', data.access_token);
    localStorage.setItem('refreshToken', data.refresh_token);
    localStorage.setItem('user', JSON.stringify(data.user));
  }

  /**
   * Clear authentication state
   */
  private clearAuthState(): void {
    this.authState.accessToken = null;
    this.authState.refreshToken = null;
    this.authState.user = null;

    localStorage.removeItem('accessToken');
    localStorage.removeItem('refreshToken');
    localStorage.removeItem('user');
  }

  /**
   * Get current auth state
   */
  getAuthState(): Readonly<AuthState> {
    return { ...this.authState };
  }

  /**
   * Check if user is authenticated
   */
  isAuthenticated(): boolean {
    return !!this.authState.accessToken && !!this.authState.user;
  }

  /**
   * Get current user
   */
  getCurrentUser(): User | null {
    return this.authState.user || null;
  }
}
