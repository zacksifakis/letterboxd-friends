// Letterboxd Friends — native macOS shell around the local Python app.
//
// Owns its own window (WKWebView), menu bar, Dock icon and ⌘Q. Starts
// ~/Desktop/letterboxd_app.py on launch and stops it on quit.
//
// Build:
//   swiftc -swift-version 5 -O main.swift -o launcher -framework Cocoa -framework WebKit

import Cocoa
import WebKit

let PORT = 8765
// The loopback IP, not "localhost": the name can resolve to IPv6 first and
// trip macOS's Local Network privacy check, which blocks the app from reaching
// its own server. 127.0.0.1 is never subject to that check.
let HOST = "127.0.0.1"
let SERVER_URL = URL(string: "http://\(HOST):\(PORT)/")!
let HOME = FileManager.default.homeDirectoryForCurrentUser.path
let SCRIPT = "\(HOME)/Desktop/letterboxd_app.py"
let LOG = "\(HOME)/letterboxd_app.log"

// MARK: - helpers

func appendLog(_ line: String) {
    let stamped = "[app \(Date())] \(line)\n"
    guard let data = stamped.data(using: .utf8) else { return }
    if let fh = FileHandle(forWritingAtPath: LOG) {
        fh.seekToEndOfFile(); fh.write(data); fh.closeFile()
    } else {
        FileManager.default.createFile(atPath: LOG, contents: data)
    }
}

var lastConnectError: String?

func httpGet(_ url: URL, timeout: TimeInterval = 1.0) -> (Int, Data?) {
    var status = 0
    var body: Data?
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: URLRequest(url: url, timeoutInterval: timeout)) { data, resp, err in
        status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        body = data
        if let err { lastConnectError = "\(err.localizedDescription) [\((err as NSError).code)]" }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + timeout + 1)
    return (status, body)
}

func serverIsUp() -> Bool { httpGet(SERVER_URL).0 == 200 }

/// Finder launches apps with a bare PATH, where /usr/bin/python3 (without the
/// scraper's packages) would win. Pick an interpreter that actually has them.
func findPython() -> String? {
    let check = "import importlib.util as u,sys; "
              + "sys.exit(0 if all(u.find_spec(m) for m in ('requests','bs4','pandas')) else 1)"
    for candidate in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
    where FileManager.default.isExecutableFile(atPath: candidate) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: candidate)
        p.arguments = ["-c", check]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { continue }
        p.waitUntilExit()
        if p.terminationStatus == 0 { return candidate }
    }
    return nil
}

func statusPage(_ message: String, isError: Bool = false) -> String {
    let safe = message
        .replacingOccurrences(of: "&", with: "&amp;")
        .replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: "\n", with: "<br>")
    return """
    <html><body style="margin:0;background:#14181c;color:\(isError ? "#ffb866" : "#9ab");
      font:15px -apple-system,sans-serif;height:100vh;display:flex;align-items:center;
      justify-content:center;text-align:center;line-height:1.6">
    <div><div style="font-size:30px;letter-spacing:6px;margin-bottom:16px">
      <span style="color:#00e054">●</span><span style="color:#40bcf4">●</span><span style="color:#ff8000">●</span>
    </div>\(safe)</div></body></html>
    """
}

// MARK: - app

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?           // only set when this app started the server

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()

        let config = WKWebViewConfiguration()
        // The page has its own "quit app" link for browser use; here ⌘Q does that.
        let hideQuit = """
        (function(){var s=document.createElement('style');
          s.textContent='#quit{display:none!important}';
          document.documentElement.appendChild(s);})();
        """
        config.userContentController.addUserScript(
            WKUserScript(source: hideQuit, injectionTime: .atDocumentEnd, forMainFrameOnly: true))

        web = WKWebView(frame: .zero, configuration: config)
        let savedZoom = UserDefaults.standard.double(forKey: "pageZoom")
        if savedZoom > 0 { web.pageZoom = CGFloat(savedZoom) }
        web.uiDelegate = self
        web.navigationDelegate = self
        web.underPageBackgroundColor = NSColor(red: 0.078, green: 0.094, blue: 0.11, alpha: 1)

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 880),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Letterboxd Friends"
        window.backgroundColor = NSColor(red: 0.078, green: 0.094, blue: 0.11, alpha: 1)
        window.minSize = NSSize(width: 720, height: 520)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("LetterboxdFriendsMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        web.loadHTMLString(statusPage("Starting…"), baseURL: nil)
        DispatchQueue.global(qos: .userInitiated).async { self.bootServer() }
    }

    func bootServer() {
        if !serverIsUp() {
            guard FileManager.default.fileExists(atPath: SCRIPT) else {
                return show("Couldn't find ~/Desktop/letterboxd_app.py", isError: true)
            }
            guard let python = findPython() else {
                return show("No python3 with requests, bs4 and pandas was found.\n\n"
                          + "In Terminal run:\npip3 install requests beautifulsoup4 pandas",
                            isError: true)
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = ["-u", SCRIPT, "--no-browser"]
            p.currentDirectoryURL = URL(fileURLWithPath: HOME)
            if !FileManager.default.fileExists(atPath: LOG) {
                FileManager.default.createFile(atPath: LOG, contents: nil)
            }
            if let fh = FileHandle(forWritingAtPath: LOG) {
                fh.seekToEndOfFile()
                p.standardOutput = fh
                p.standardError = fh
            }
            do { try p.run() } catch {
                return show("Couldn't start the server:\n\(error.localizedDescription)", isError: true)
            }
            server = p

            var up = false
            for _ in 0..<160 {                      // up to ~40s
                if serverIsUp() { up = true; break }
                if !p.isRunning { break }
                Thread.sleep(forTimeInterval: 0.25)
            }
            guard up else {
                let why = lastConnectError ?? "no response"
                appendLog("gave up waiting for server: \(why); python still running: \(p.isRunning)")
                return show("Couldn't connect to the app's server.\n\(why)\n\n"
                          + "Details are in ~/letterboxd_app.log", isError: true)
            }
        }
        DispatchQueue.main.async { self.web.load(URLRequest(url: SERVER_URL)) }
    }

    func show(_ message: String, isError: Bool = false) {
        DispatchQueue.main.async {
            self.web.loadHTMLString(statusPage(message, isError: isError), baseURL: nil)
        }
    }

    // MARK: menus

    func buildMenu() {
        let bar = NSMenu()

        let appItem = NSMenuItem(); bar.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Letterboxd Friends",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Letterboxd Friends",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Letterboxd Friends",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        // Without an Edit menu, ⌘C / ⌘V / ⌘A don't work in the search boxes.
        let editItem = NSMenuItem(); bar.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); bar.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        view.addItem(.separator())
        view.addItem(withTitle: "Actual Size", action: #selector(zoomReset), keyEquivalent: "0")
        view.addItem(withTitle: "Zoom In", action: #selector(zoomIn), keyEquivalent: "+")
        // ⌘= too: on most keyboards "+" needs Shift, so ⌘= is what people press
        let zoomInAlt = view.addItem(withTitle: "Zoom In", action: #selector(zoomIn), keyEquivalent: "=")
        zoomInAlt.isAlternate = true
        zoomInAlt.keyEquivalentModifierMask = [.command]
        view.addItem(withTitle: "Zoom Out", action: #selector(zoomOut), keyEquivalent: "-")
        viewItem.submenu = view

        let windowItem = NSMenuItem(); bar.addItem(windowItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = win
        NSApp.windowsMenu = win

        NSApp.mainMenu = bar
    }

    // MARK: zoom (⌘+ / ⌘- / ⌘0), remembered between launches

    let zoomSteps: [CGFloat] = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]

    func setZoom(_ z: CGFloat) {
        web.pageZoom = z
        UserDefaults.standard.set(Double(z), forKey: "pageZoom")
    }

    @objc func zoomIn() {
        setZoom(zoomSteps.first(where: { $0 > web.pageZoom + 0.001 }) ?? zoomSteps.last!)
    }

    @objc func zoomOut() {
        setZoom(zoomSteps.last(where: { $0 < web.pageZoom - 0.001 }) ?? zoomSteps.first!)
    }

    @objc func zoomReset() { setZoom(1.0) }

    @objc func reload() {
        if web.url?.host == HOST {
            web.reload()
        } else {
            web.loadHTMLString(statusPage("Starting…"), baseURL: nil)
            DispatchQueue.global(qos: .userInitiated).async { self.bootServer() }
        }
    }

    // MARK: lifecycle

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // The scrape runs as a child of the server and dies with it, so warn first.
        guard server?.isRunning == true, scrapeRunning() else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "A scrape is still running"
        alert.informativeText = "Quitting now stops it, and nothing from this run will be saved."
        alert.addButton(withTitle: "Keep Running")
        alert.addButton(withTitle: "Quit Anyway")
        return alert.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let p = server, p.isRunning { p.terminate() }
    }

    func scrapeRunning() -> Bool {
        let (status, data) = httpGet(URL(string: "http://\(HOST):\(PORT)/api/status")!)
        guard status == 200, let data,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return (json["running"] as? Bool) == true
    }

    // MARK: JavaScript dialogs (WKWebView ignores alert/confirm unless handled)

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    // MARK: links and downloads

    /// target="_blank" links (film pages) open in your normal browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { return decisionHandler(.allow) }
        let local = url.host == "localhost" || url.host == "127.0.0.1"

        if local && url.path == "/api/download" {
            decisionHandler(.cancel)
            saveDownload(url)
            return
        }
        if !local && (url.scheme == "http" || url.scheme == "https")
            && navigationAction.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    /// "download CSV" -> ~/Downloads, then reveal it in Finder.
    func saveDownload(_ url: URL) {
        URLSession.shared.dataTask(with: url) { data, response, error in
            guard let data, error == nil else { return }
            var name = "letterboxd.csv"
            if let cd = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition"),
               let start = cd.range(of: "filename=\"") {
                let rest = cd[start.upperBound...]
                if let end = rest.firstIndex(of: "\"") { name = String(rest[..<end]) }
            }
            let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
            let base = (name as NSString).deletingPathExtension
            let ext = (name as NSString).pathExtension
            var dest = downloads.appendingPathComponent(name)
            var n = 1
            while FileManager.default.fileExists(atPath: dest.path) {
                dest = downloads.appendingPathComponent("\(base) (\(n)).\(ext)")
                n += 1
            }
            guard (try? data.write(to: dest)) != nil else { return }
            DispatchQueue.main.async { NSWorkspace.shared.activateFileViewerSelecting([dest]) }
        }.resume()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
