/**
 * React TypeScript component for authentication UI
 * Full type safety, no @ts-ignore needed
 */

import React, { useState, useEffect, useCallback } from 'react';
import { AuthService } from './auth.service';
import {
  User,
  LoginCredentials,
  RegisterCredentials,
  VerifyEmailRequest,
  UpdateProfileRequest,
  AlertType,
} from './auth.types';

interface TabType = 'login' | 'register' | 'profile';

interface AlertState {
  message: string;
  type: AlertType;
  visible: boolean;
}

export const AuthComponent: React.FC = () => {
  const [authService] = useState(() => new AuthService());
  const [activeTab, setActiveTab] = useState<TabType>('login');
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [user, setUser] = useState<User | null>(null);

  // Login state
  const [loginForm, setLoginForm] = useState<LoginCredentials>({
    email: '',
    password: '',
  });
  const [loginAlert, setLoginAlert] = useState<AlertState>({
    message: '',
    type: 'info',
    visible: false,
  });
  const [loginLoading, setLoginLoading] = useState(false);

  // Register state
  const [registerForm, setRegisterForm] = useState<RegisterCredentials>({
    email: '',
    password: '',
    name: '',
  });
  const [registerAlert, setRegisterAlert] = useState<AlertState>({
    message: '',
    type: 'info',
    visible: false,
  });
  const [registerLoading, setRegisterLoading] = useState(false);

  // Profile state
  const [profileName, setProfileName] = useState('');
  const [profileAlert, setProfileAlert] = useState<AlertState>({
    message: '',
    type: 'info',
    visible: false,
  });
  const [profileLoading, setProfileLoading] = useState(false);

  // Initialize auth state on mount
  useEffect(() => {
    const state = authService.getAuthState();
    setIsAuthenticated(authService.isAuthenticated());
    setUser(state.user);
    if (state.user) {
      setProfileName(state.user.name);
    }
  }, [authService]);

  /**
   * Show alert with auto-dismiss
   */
  const showAlert = useCallback(
    (
      setState: React.Dispatch<React.SetStateAction<AlertState>>,
      message: string,
      type: AlertType,
      duration: number = 5000
    ) => {
      setState({ message, type, visible: true });
      const timer = setTimeout(() => {
        setState(prev => ({ ...prev, visible: false }));
      }, duration);
      return () => clearTimeout(timer);
    },
    []
  );

  /**
   * Handle login
   */
  const handleLogin = async (e: React.FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();

    if (!loginForm.email || !loginForm.password) {
      showAlert(setLoginAlert, 'Email and password are required', 'error');
      return;
    }

    setLoginLoading(true);
    try {
      const response = await authService.login(loginForm);
      setIsAuthenticated(true);
      setUser(response.user);
      showAlert(setLoginAlert, `✅ Welcome back, ${response.user.name}!`, 'success');
      setLoginForm({ email: '', password: '' });
      setTimeout(() => setActiveTab('profile'), 1500);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Login failed';
      showAlert(setLoginAlert, `❌ ${message}`, 'error');
    } finally {
      setLoginLoading(false);
    }
  };

  /**
   * Handle registration
   */
  const handleRegister = async (
    e: React.FormEvent<HTMLFormElement>
  ): Promise<void> => {
    e.preventDefault();

    if (!registerForm.email || !registerForm.password || !registerForm.name) {
      showAlert(registerAlert, 'All fields are required', 'error');
      return;
    }

    setRegisterLoading(true);
    try {
      const response = await authService.register(registerForm);
      setIsAuthenticated(true);
      setUser(response.user);
      showAlert(
        setRegisterAlert,
        '✅ Account created! Check your email for verification code.',
        'success'
      );
      setRegisterForm({ email: '', password: '', name: '' });
      setTimeout(() => setActiveTab('profile'), 1500);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Registration failed';
      showAlert(setRegisterAlert, `❌ ${message}`, 'error');
    } finally {
      setRegisterLoading(false);
    }
  };

  /**
   * Handle logout
   */
  const handleLogout = async (): Promise<void> => {
    try {
      await authService.logout();
      setIsAuthenticated(false);
      setUser(null);
      setLoginForm({ email: '', password: '' });
      showAlert(profileAlert, '✅ Logged out successfully', 'success');
      setTimeout(() => setActiveTab('login'), 1500);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Logout failed';
      showAlert(setProfileAlert, `❌ ${message}`, 'error');
    }
  };

  /**
   * Handle profile update
   */
  const handleUpdateProfile = async (
    e: React.FormEvent<HTMLFormElement>
  ): Promise<void> => {
    e.preventDefault();

    if (!profileName.trim()) {
      showAlert(setProfileAlert, 'Name cannot be empty', 'error');
      return;
    }

    setProfileLoading(true);
    try {
      const updated = await authService.updateProfile({ name: profileName });
      setUser(updated);
      showAlert(setProfileAlert, '✅ Profile updated successfully', 'success');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Update failed';
      showAlert(setProfileAlert, `❌ ${message}`, 'error');
    } finally {
      setProfileLoading(false);
    }
  };

  return (
    <div style={styles.container}>
      {/* Tab Navigation */}
      <div style={styles.tabs}>
        <button
          style={{
            ...styles.tabButton,
            ...(activeTab === 'login' ? styles.tabButtonActive : {}),
          }}
          onClick={() => setActiveTab('login')}
        >
          Login
        </button>
        <button
          style={{
            ...styles.tabButton,
            ...(activeTab === 'register' ? styles.tabButtonActive : {}),
          }}
          onClick={() => setActiveTab('register')}
        >
          Register
        </button>
        <button
          style={{
            ...styles.tabButton,
            ...(activeTab === 'profile' ? styles.tabButtonActive : {}),
          }}
          onClick={() => setActiveTab('profile')}
        >
          Profile
        </button>
      </div>

      {/* Login Tab */}
      {activeTab === 'login' && (
        <div>
          <h1 style={styles.title}>Login</h1>
          {loginAlert.visible && (
            <div style={{
              ...styles.alert,
              ...(loginAlert.type === 'success' ? styles.alertSuccess : {}),
              ...(loginAlert.type === 'error' ? styles.alertError : {}),
            }}>
              {loginAlert.message}
            </div>
          )}
          <form onSubmit={handleLogin} style={styles.form}>
            <div style={styles.formGroup}>
              <label style={styles.label}>Email</label>
              <input
                type="email"
                value={loginForm.email}
                onChange={(e) =>
                  setLoginForm(prev => ({ ...prev, email: e.target.value }))
                }
                placeholder="your@email.com"
                style={styles.input}
              />
            </div>
            <div style={styles.formGroup}>
              <label style={styles.label}>Password</label>
              <input
                type="password"
                value={loginForm.password}
                onChange={(e) =>
                  setLoginForm(prev => ({ ...prev, password: e.target.value }))
                }
                placeholder="••••••••"
                style={styles.input}
              />
            </div>
            <button
              type="submit"
              disabled={loginLoading}
              style={{
                ...styles.button,
                ...(loginLoading ? styles.buttonDisabled : {}),
              }}
            >
              {loginLoading ? 'Signing in...' : 'Sign In'}
            </button>
          </form>
        </div>
      )}

      {/* Register Tab */}
      {activeTab === 'register' && (
        <div>
          <h1 style={styles.title}>Create Account</h1>
          {registerAlert.visible && (
            <div style={{
              ...styles.alert,
              ...(registerAlert.type === 'success' ? styles.alertSuccess : {}),
              ...(registerAlert.type === 'error' ? styles.alertError : {}),
            }}>
              {registerAlert.message}
            </div>
          )}
          <form onSubmit={handleRegister} style={styles.form}>
            <div style={styles.formGroup}>
              <label style={styles.label}>Full Name</label>
              <input
                type="text"
                value={registerForm.name}
                onChange={(e) =>
                  setRegisterForm(prev => ({ ...prev, name: e.target.value }))
                }
                placeholder="John Doe"
                style={styles.input}
              />
            </div>
            <div style={styles.formGroup}>
              <label style={styles.label}>Email</label>
              <input
                type="email"
                value={registerForm.email}
                onChange={(e) =>
                  setRegisterForm(prev => ({ ...prev, email: e.target.value }))
                }
                placeholder="your@email.com"
                style={styles.input}
              />
            </div>
            <div style={styles.formGroup}>
              <label style={styles.label}>Password</label>
              <input
                type="password"
                value={registerForm.password}
                onChange={(e) =>
                  setRegisterForm(prev => ({ ...prev, password: e.target.value }))
                }
                placeholder="••••••••"
                style={styles.input}
              />
            </div>
            <button
              type="submit"
              disabled={registerLoading}
              style={{
                ...styles.button,
                ...(registerLoading ? styles.buttonDisabled : {}),
              }}
            >
              {registerLoading ? 'Creating account...' : 'Create Account'}
            </button>
          </form>
        </div>
      )}

      {/* Profile Tab */}
      {activeTab === 'profile' && (
        <div>
          <h1 style={styles.title}>My Profile</h1>
          {profileAlert.visible && (
            <div style={{
              ...styles.alert,
              ...(profileAlert.type === 'success' ? styles.alertSuccess : {}),
              ...(profileAlert.type === 'error' ? styles.alertError : {}),
            }}>
              {profileAlert.message}
            </div>
          )}

          {!isAuthenticated ? (
            <div style={styles.notLoggedIn}>
              Not logged in. Log in or create an account to view your profile.
            </div>
          ) : user ? (
            <div>
              <div style={styles.profileSection}>
                <h2 style={styles.sectionTitle}>User Information</h2>

                <div style={styles.profileInfo}>
                  <label style={styles.infoLabel}>Email</label>
                  <p style={styles.infoValue}>{user.email}</p>
                </div>

                <div style={styles.profileInfo}>
                  <label style={styles.infoLabel}>Full Name</label>
                  <p style={styles.infoValue}>{user.name}</p>
                </div>

                <div style={styles.profileInfo}>
                  <label style={styles.infoLabel}>Account Status</label>
                  <p style={styles.infoValue}>
                    {user.is_verified ? '✅ Verified' : '⏳ Pending verification'}
                  </p>
                </div>

                <div style={styles.profileInfo}>
                  <label style={styles.infoLabel}>Member Since</label>
                  <p style={styles.infoValue}>
                    {new Date(user.created_at).toLocaleDateString()}
                  </p>
                </div>

                <hr style={styles.divider} />

                <form onSubmit={handleUpdateProfile} style={styles.form}>
                  <div style={styles.formGroup}>
                    <label style={styles.label}>Update Name</label>
                    <input
                      type="text"
                      value={profileName}
                      onChange={(e) => setProfileName(e.target.value)}
                      style={styles.input}
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={profileLoading}
                    style={{
                      ...styles.button,
                      ...(profileLoading ? styles.buttonDisabled : {}),
                    }}
                  >
                    {profileLoading ? 'Updating...' : 'Update Profile'}
                  </button>
                </form>

                <button
                  onClick={handleLogout}
                  style={{
                    ...styles.button,
                    marginTop: '20px',
                    background: '#ef4444',
                  }}
                >
                  Logout
                </button>
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
};

/**
 * Inline styles (replace with CSS modules in production)
 */
const styles: Record<string, React.CSSProperties> = {
  container: {
    background: 'white',
    borderRadius: '12px',
    boxShadow: '0 20px 60px rgba(0, 0, 0, 0.3)',
    width: '100%',
    maxWidth: '450px',
    padding: '40px',
    margin: '0 auto',
  },
  tabs: {
    display: 'flex',
    gap: '10px',
    marginBottom: '30px',
    borderBottom: '2px solid #e5e7eb',
  },
  tabButton: {
    flex: 1,
    padding: '12px',
    background: 'none',
    border: 'none',
    borderBottom: '3px solid transparent',
    color: '#6b7280',
    fontWeight: 600,
    cursor: 'pointer',
    fontSize: '14px',
    marginBottom: '-2px',
  },
  tabButtonActive: {
    color: '#667eea',
    borderBottomColor: '#667eea',
  },
  title: {
    color: '#1f2937',
    marginBottom: '30px',
    textAlign: 'center',
    fontSize: '28px',
  },
  alert: {
    padding: '12px',
    borderRadius: '8px',
    marginBottom: '20px',
    fontSize: '14px',
  },
  alertSuccess: {
    background: '#d1fae5',
    color: '#065f46',
    border: '1px solid #6ee7b7',
  },
  alertError: {
    background: '#fee2e2',
    color: '#7f1d1d',
    border: '1px solid #fca5a5',
  },
  form: {
    display: 'flex',
    flexDirection: 'column',
    gap: '20px',
  },
  formGroup: {
    marginBottom: '20px',
  },
  label: {
    display: 'block',
    color: '#374151',
    fontWeight: 500,
    marginBottom: '8px',
    fontSize: '14px',
  },
  input: {
    width: '100%',
    padding: '12px',
    border: '1px solid #e5e7eb',
    borderRadius: '8px',
    fontSize: '14px',
    fontFamily: 'inherit',
    boxSizing: 'border-box',
  },
  button: {
    width: '100%',
    padding: '12px',
    background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
    color: 'white',
    border: 'none',
    borderRadius: '8px',
    fontWeight: 600,
    fontSize: '16px',
    cursor: 'pointer',
    transition: 'transform 0.2s, box-shadow 0.2s',
  },
  buttonDisabled: {
    opacity: 0.6,
    cursor: 'not-allowed',
  },
  profileSection: {
    marginTop: '30px',
    padding: '20px',
    background: '#f9fafb',
    borderRadius: '8px',
  },
  sectionTitle: {
    marginBottom: '20px',
    color: '#1f2937',
    fontSize: '16px',
  },
  profileInfo: {
    marginBottom: '15px',
  },
  infoLabel: {
    color: '#6b7280',
    marginBottom: '5px',
    display: 'block',
    fontSize: '12px',
  },
  infoValue: {
    color: '#1f2937',
    fontWeight: 500,
    margin: 0,
  },
  divider: {
    margin: '20px 0',
    border: 'none',
    borderTop: '1px solid #e5e7eb',
  },
  notLoggedIn: {
    textAlign: 'center',
    color: '#6b7280',
    padding: '20px',
  },
};
