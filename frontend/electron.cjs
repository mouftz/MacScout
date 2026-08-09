const { app, BrowserWindow, globalShortcut, screen } = require('electron');

// Set app name early — affects macOS menu bar, dock, About panel, etc.
app.setName('MacScout');

let win;

function createWindow() {
    const { width, height } = screen.getPrimaryDisplay().workAreaSize;
    
    win = new BrowserWindow({
        title: 'MacScout',
        width: width,
        height: height,
        x: 0,
        y: 0,
        transparent: true,
        frame: false,
        alwaysOnTop: true,
        resizable: true,
        hasShadow: false,
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
        },
    });

    win.loadURL('http://localhost:5174');

    // Opt-in only: a detached DevTools window on top of the game is not what
    // you want while playing. Run with MACSCOUT_DEVTOOLS=1 to debug.
    if (process.env.MACSCOUT_DEVTOOLS === '1') {
        win.webContents.openDevTools({ mode: 'detach' });
    }

    // Make the window click-through everywhere
    win.setIgnoreMouseEvents(true, { forward: true });
    
    // Keep visible when League goes fullscreen on macOS
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
}

app.whenReady().then(() => {
    createWindow();

    // Cmd+Shift+H -> toggle visibility
    globalShortcut.register('CommandOrControl+Shift+H', () => {
        if (win.isVisible()) win.hide();
        else win.show();
    });

    // Cmd+Shift+C -> collapse to a small pill / expand again.
    // Dispatching straight into the page avoids needing a preload + IPC bridge
    // just to flip one boolean.
    globalShortcut.register('CommandOrControl+Shift+C', () => {
        win.webContents.executeJavaScript(
            'window.dispatchEvent(new CustomEvent("macscout:toggle-collapse"))'
        ).catch(() => {});
    });

    // Cmd+Shift+Q -> quit the overlay
    globalShortcut.register('CommandOrControl+Shift+Q', () => {
        app.quit();
    });
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
});

app.on('will-quit', () => {
    globalShortcut.unregisterAll();
});