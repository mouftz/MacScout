const { app, BrowserWindow, globalShortcut, screen } = require('electron');
const { exec } = require('child_process');

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
        // Set here, not via setFullScreenable() later, which would clobber
        // the collectionBehavior that setVisibleOnAllWorkspaces depends on.
        fullscreenable: false,
        skipTaskbar: true,
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

    // Order matters on macOS. Both setAlwaysOnTop and setFullScreenable
    // rewrite the window's collectionBehavior, which clears the
    // canJoinAllSpaces / fullScreenAuxiliary flags that setVisibleOnAllWorkspaces
    // sets — so that call has to come LAST or the overlay stops drawing over
    // borderless League. (fullscreenable is set in the constructor above.)
    //
    // 'screen-saver' is the highest practical window level; plain
    // `alwaysOnTop: true` uses 'floating', which sits below a fullscreen app.
    win.setAlwaysOnTop(true, 'screen-saver');
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

    watchFrontmostApp();
}

// --- Hide the overlay when League isn't the app you're looking at ----------
// The window is always-on-top at screen-saver level, so without this it floats
// over Discord, the browser, everything. lsappinfo needs no accessibility
// permission, unlike the System Events AppleScript route.
let manuallyHidden = false;

function watchFrontmostApp() {
    setInterval(() => {
        if (!win || win.isDestroyed() || manuallyHidden) return;

        exec('lsappinfo info -only name "$(lsappinfo front)"', (err, stdout) => {
            if (err) return;
            const isLeague = /league/i.test(stdout);
            if (isLeague && !win.isVisible()) {
                win.showInactive();  // never steal focus from the game
            } else if (!isLeague && win.isVisible()) {
                win.hide();
            }
        });
    }, 1000);
}

app.whenReady().then(() => {
    createWindow();

    // Cmd+Shift+H -> toggle visibility
    globalShortcut.register('CommandOrControl+Shift+H', () => {
        if (win.isVisible()) {
            manuallyHidden = true;   // keep it hidden even while League is focused
            win.hide();
        } else {
            manuallyHidden = false;
            win.showInactive();
        }
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