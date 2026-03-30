# React Frontend Agent — Skill Specification

You are implementing an enterprise React/TypeScript application from a WPF migration PRD.
Follow every rule in this document exactly. Do not deviate.

---

## 1. Technology Stack (MANDATORY — no substitutions)

| Concern | Library | Version |
|---------|---------|---------|
| UI Framework | React | 18.x |
| Language | TypeScript | 5.x |
| Build Tool | Vite | 5.x |
| **Grid** | **AG Grid Enterprise** | **^31** |
| UI Components | MUI (Material UI) | ^5 |
| Theming | MUI `createTheme` + CSS variables | — |
| State Management | Redux Toolkit (RTK) | ^2 |
| API Layer | Axios | ^1 |
| Mock Interception | MSW (Mock Service Worker) | ^2 |
| Routing | React Router v6 | ^6 |
| Icons | `@mui/icons-material` | ^5 |

All of these are peer dependencies. Always include them in the generated `package.json`.

---

## 2. Project File Structure

Generate exactly this structure:

```
src/
  types/          # TypeScript interfaces & enums
    index.ts
  theme/          # Theme definitions (light + dark)
    theme.ts
    ThemeProvider.tsx
  mocks/          # All mock data and MSW handlers
    mockData.ts
    handlers.ts
    browser.ts
  store/          # Redux Toolkit slices
    store.ts
    <feature>Slice.ts
  api/            # API service modules
    <feature>Api.ts
  components/     # Feature components
    grid/
      <Feature>Grid.tsx      # Every grid gets its own file
    <Feature>View.tsx        # Top-level view/page
  App.tsx
  main.tsx
package.json
vite.config.ts
```

---

## 3. Theming (MANDATORY)

### 3.1 Theme Definition — `src/theme/theme.ts`

Create TWO themes: `lightTheme` and `darkTheme` using MUI `createTheme`.

```typescript
import { createTheme, Theme } from '@mui/material/styles';

const baseTokens = {
  borderRadius: 4,
  fontFamily: '"Inter", "Roboto", "Helvetica Neue", sans-serif',
};

export const lightTheme: Theme = createTheme({
  palette: {
    mode: 'light',
    primary:   { main: '#1976d2' },
    secondary: { main: '#9c27b0' },
    background: { default: '#f5f5f5', paper: '#ffffff' },
    text: { primary: '#212121', secondary: '#757575' },
  },
  typography: { fontFamily: baseTokens.fontFamily },
  shape: { borderRadius: baseTokens.borderRadius },
});

export const darkTheme: Theme = createTheme({
  palette: {
    mode: 'dark',
    primary:   { main: '#90caf9' },
    secondary: { main: '#ce93d8' },
    background: { default: '#121212', paper: '#1e1e1e' },
    text: { primary: '#ffffff', secondary: '#b0b0b0' },
  },
  typography: { fontFamily: baseTokens.fontFamily },
  shape: { borderRadius: baseTokens.borderRadius },
});
```

### 3.2 Theme Provider — `src/theme/ThemeProvider.tsx`

```typescript
import React, { createContext, useContext, useState, useMemo } from 'react';
import { ThemeProvider as MuiThemeProvider, CssBaseline } from '@mui/material';
import { lightTheme, darkTheme } from './theme';

type ThemeMode = 'light' | 'dark';
interface ThemeContextType { mode: ThemeMode; toggle: () => void; }
const ThemeContext = createContext<ThemeContextType>({ mode: 'light', toggle: () => {} });
export const useThemeMode = () => useContext(ThemeContext);

export const AppThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [mode, setMode] = useState<ThemeMode>('light');
  const theme = useMemo(() => mode === 'light' ? lightTheme : darkTheme, [mode]);
  return (
    <ThemeContext.Provider value={{ mode, toggle: () => setMode(m => m === 'light' ? 'dark' : 'light') }}>
      <MuiThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </MuiThemeProvider>
    </ThemeContext.Provider>
  );
};
```

### 3.3 Theme Toggle Button

Every layout/navbar component MUST include a theme toggle:
```typescript
import { IconButton, Tooltip } from '@mui/material';
import { Brightness4, Brightness7 } from '@mui/icons-material';
import { useThemeMode } from '../theme/ThemeProvider';

const { mode, toggle } = useThemeMode();
// In JSX:
<Tooltip title={`Switch to ${mode === 'light' ? 'dark' : 'light'} mode`}>
  <IconButton onClick={toggle} color="inherit">
    {mode === 'light' ? <Brightness4 /> : <Brightness7 />}
  </IconButton>
</Tooltip>
```

---

## 4. AG Grid Enterprise — Rules (MANDATORY)

### 4.1 Setup

Every grid file MUST import the AG Grid Enterprise module at the top:
```typescript
import { LicenseManager } from 'ag-grid-enterprise';
import 'ag-grid-community/styles/ag-grid.css';
import 'ag-grid-community/styles/ag-theme-quartz.css';

// Set to empty string for dev — enterprise features still activate in trial mode
LicenseManager.setLicenseKey('');
```

### 4.2 Grid Theme Integration

Sync AG Grid theme with the MUI dark/light mode:
```typescript
import { useThemeMode } from '../../theme/ThemeProvider';

const { mode } = useThemeMode();
const agThemeClass = mode === 'dark' ? 'ag-theme-quartz-dark' : 'ag-theme-quartz';
```

Apply the class to the grid wrapper:
```tsx
<div className={agThemeClass} style={{ height: 500, width: '100%' }}>
  <AgGridReact ... />
</div>
```

### 4.3 Context Menu (MANDATORY for every grid)

Every `AgGridReact` component MUST include a context menu with at minimum these actions:

```typescript
const getContextMenuItems = useCallback((params: GetContextMenuItemsParams): MenuItemDef[] => {
  return [
    {
      name: 'View Details',
      icon: '<span class="ag-icon ag-icon-eye"></span>',
      action: () => { if (params.node?.data) handleViewDetails(params.node.data); },
    },
    {
      name: 'Edit',
      icon: '<span class="ag-icon ag-icon-edit"></span>',
      action: () => { if (params.node?.data) handleEdit(params.node.data); },
    },
    'separator',
    {
      name: 'Copy',
      shortcut: 'Ctrl+C',
      action: () => params.api.copyToClipboard(),
    },
    {
      name: 'Export',
      subMenu: [
        { name: 'Export CSV',   action: () => params.api.exportDataAsCsv() },
        { name: 'Export Excel', action: () => params.api.exportDataAsExcel() },
      ],
    },
  ];
}, []);

// Add to AgGridReact:
// getContextMenuItems={getContextMenuItems}
```

### 4.4 Standard Grid Props

All grids MUST use these base props (add domain-specific columnDefs on top):

```typescript
<AgGridReact
  rowData={rowData}
  columnDefs={columnDefs}
  defaultColDef={{
    sortable: true,
    filter: true,
    resizable: true,
    floatingFilter: true,
    suppressMenu: false,
    flex: 1,
    minWidth: 100,
  }}
  pagination={true}
  paginationPageSize={25}
  paginationPageSizeSelector={[10, 25, 50, 100]}
  rowSelection="multiple"
  enableRangeSelection={true}
  enableClipboard={true}
  sideBar={true}
  statusBar={{
    statusPanels: [
      { statusPanel: 'agTotalAndFilteredRowCountComponent', align: 'left' },
      { statusPanel: 'agSelectedRowCountComponent', align: 'center' },
      { statusPanel: 'agAggregationComponent', align: 'right' },
    ],
  }}
  getContextMenuItems={getContextMenuItems}
  animateRows={true}
  suppressRowClickSelection={true}
/>
```

---

## 5. Standard MUI Component Patterns

Use ONLY these MUI components across all generated files (do not invent patterns):

| Use Case | Component |
|----------|-----------|
| Page layout | `Box`, `Container`, `Stack` |
| Cards / panels | `Paper` with `elevation={1}` and `sx={{ p: 2, borderRadius: 2 }}` |
| Toolbar | `AppBar` + `Toolbar` |
| Buttons | `Button` (variant="contained" for primary, "outlined" for secondary) |
| Form fields | `TextField` with `size="small"` |
| Dropdowns | `Select` + `MenuItem` inside `FormControl` |
| Dialogs | `Dialog` + `DialogTitle` + `DialogContent` + `DialogActions` |
| Tabs | `Tabs` + `Tab` |
| Status chips | `Chip` (color="success"/"error"/"warning"/"default") |
| Loading | `CircularProgress` centered in a `Box` with `display: flex; justifyContent: center` |
| Empty state | `Typography variant="body2" color="text.secondary"` inside centered `Box` |
| Notifications | `Snackbar` + `Alert` |

---

## 6. Mock Data & API (MANDATORY)

### 6.1 `src/mocks/mockData.ts`

- Export a `const` for every entity array in the PRD
- Records must be realistic — **never use placeholder values** like "Item 1" or "Test Name"
- Every array must have at least **8 records**
- Include realistic dates (use last 90 days), amounts, names, statuses from the domain

```typescript
import { YourEntityType } from '../types';

export const mockOrders: YourEntityType[] = [
  { id: 'ORD-2024-001', customerName: 'Acme Corporation', amount: 4250.00, status: 'Active', createdAt: '2024-01-15T09:23:00Z' },
  // ... 7+ more records
];
```

### 6.2 `src/mocks/handlers.ts`

Use MSW to intercept ALL API calls:

```typescript
import { http, HttpResponse } from 'msw';
import { mockOrders } from './mockData';

export const handlers = [
  http.get('/api/orders', () => HttpResponse.json(mockOrders)),
  http.get('/api/orders/:id', ({ params }) => {
    const order = mockOrders.find(o => o.id === params.id);
    return order ? HttpResponse.json(order) : new HttpResponse(null, { status: 404 });
  }),
  http.post('/api/orders', async ({ request }) => {
    const body = await request.json() as Partial<YourEntityType>;
    return HttpResponse.json({ ...body, id: `ORD-${Date.now()}` }, { status: 201 });
  }),
  // Add GET/POST/PUT/DELETE for every entity
];
```

### 6.3 `src/mocks/browser.ts`

```typescript
import { setupWorker } from 'msw/browser';
import { handlers } from './handlers';
export const worker = setupWorker(...handlers);
```

### 6.4 `src/main.tsx` — Activate mocks in development

```typescript
async function enableMocking() {
  if (import.meta.env.DEV) {
    const { worker } = await import('./mocks/browser');
    return worker.start({ onUnhandledRequest: 'bypass' });
  }
}
enableMocking().then(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode><App /></React.StrictMode>
  );
});
```

### 6.5 API Services — `USE_MOCK` toggle

```typescript
// src/api/ordersApi.ts
import axios from 'axios';
import { mockOrders } from '../mocks/mockData';
import type { OrderType } from '../types';

const USE_MOCK = import.meta.env.DEV;  // true in dev, false in prod

export const fetchOrders = async (): Promise<OrderType[]> => {
  if (USE_MOCK) return Promise.resolve(mockOrders);
  const { data } = await axios.get<OrderType[]>('/api/orders');
  return data;
};
```

---

## 7. `package.json` Template

Always generate this `package.json`:

```json
{
  "name": "wpf-migration-app",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.23.0",
    "@mui/material": "^5.16.0",
    "@mui/icons-material": "^5.16.0",
    "@emotion/react": "^11.13.0",
    "@emotion/styled": "^11.13.0",
    "ag-grid-community": "^31.3.0",
    "ag-grid-enterprise": "^31.3.0",
    "ag-grid-react": "^31.3.0",
    "@reduxjs/toolkit": "^2.3.0",
    "react-redux": "^9.1.0",
    "axios": "^1.7.0",
    "msw": "^2.3.0"
  },
  "devDependencies": {
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "typescript": "^5.4.0",
    "vite": "^5.3.0"
  }
}
```

---

## 8. Implementation Checklist

Before considering your work complete, verify:
- [ ] `package.json` created with all dependencies above
- [ ] `src/types/index.ts` — all interfaces and enums from PRD
- [ ] `src/theme/theme.ts` — lightTheme and darkTheme exported
- [ ] `src/theme/ThemeProvider.tsx` — toggle context created
- [ ] `src/mocks/mockData.ts` — 8+ records per entity, realistic values
- [ ] `src/mocks/handlers.ts` — GET/POST/PUT/DELETE for each entity
- [ ] `src/mocks/browser.ts` — MSW worker setup
- [ ] `src/store/store.ts` + one slice per entity
- [ ] `src/api/*.ts` — USE_MOCK flag in every service
- [ ] `src/components/grid/*.tsx` — AG Grid with context menu + theme sync
- [ ] All view components wired to Redux state (not to raw mock arrays)
- [ ] `src/App.tsx` — router + `AppThemeProvider` wrapping everything
- [ ] `src/main.tsx` — `enableMocking()` + conditional MSW activation
