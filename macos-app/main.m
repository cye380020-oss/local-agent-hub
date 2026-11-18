#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <CommonCrypto/CommonHMAC.h>
#import <CommonCrypto/CommonDigest.h>

@interface HubApp : NSObject <NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate>
@property NSWindow *window;
@property WKWebView *web;
@property NSTextField *status;
@property NSButton *retry;
@property NSString *root;
@property NSString *expectedVersion;
@property NSInteger port;
@property BOOL busy;
@property BOOL spawned;
@property NSInteger attempts;
@end

@implementation HubApp
- (NSInteger)preferredPort {
    NSString *portFile = [self.root stringByAppendingPathComponent:@"desktop-v2.port"];
    NSString *raw = [NSString stringWithContentsOfFile:portFile encoding:NSUTF8StringEncoding error:nil];
    NSInteger val = [[raw stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]] integerValue];
    if (val >= 8765 && val <= 8785) return val;
    return 8765;
}
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSString *ver = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"CFBundleShortVersionString"];
    self.expectedVersion = ([ver isKindOfClass:[NSString class]] && ver.length > 0) ? ver : @"2.1.0";

    NSString *configured = [[NSBundle mainBundle] objectForInfoDictionaryKey:@"HubWorkspace"];
    if (![configured isKindOfClass:[NSString class]] || configured.length == 0) {
        configured = @"~/Library/Application Support/LocalAgentHub/workspace";
    }
    self.root = [configured stringByExpandingTildeInPath];
    [[NSFileManager defaultManager] createDirectoryAtPath:self.root withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:nil];
    self.port = [self preferredPort];

    NSMenu *menu = [NSMenu new];
    NSMenuItem *mainItem = [NSMenuItem new];
    [menu addItem:mainItem];
    NSMenu *appMenu = [NSMenu new];
    [appMenu addItemWithTitle:@"关于本地 Agent 工作台" action:@selector(about:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"重新连接工作台" action:@selector(connect:) keyEquivalent:@"r"];
    [appMenu addItemWithTitle:@"打开任务文件夹" action:@selector(showFiles:) keyEquivalent:@"o"];
    [appMenu addItemWithTitle:@"打开辅助功能权限设置" action:@selector(openAccessibilitySettings:) keyEquivalent:@""];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"退出工作台窗口" action:@selector(terminate:) keyEquivalent:@"q"];
    mainItem.submenu = appMenu;
    NSMenuItem *editItem = [NSMenuItem new];
    [menu addItem:editItem];
    NSMenu *edit = [[NSMenu alloc] initWithTitle:@"编辑"];
    [edit addItemWithTitle:@"撤销" action:@selector(undo:) keyEquivalent:@"z"];
    [edit addItemWithTitle:@"剪切" action:@selector(cut:) keyEquivalent:@"x"];
    [edit addItemWithTitle:@"复制" action:@selector(copy:) keyEquivalent:@"c"];
    [edit addItemWithTitle:@"粘贴" action:@selector(paste:) keyEquivalent:@"v"];
    [edit addItemWithTitle:@"全选" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = edit;
    NSApp.mainMenu = menu;

    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0,0,1240,840) styleMask:NSWindowStyleMaskTitled|NSWindowStyleMaskClosable|NSWindowStyleMaskMiniaturizable|NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    self.window.title = [NSString stringWithFormat:@"本地 Agent 协同工作台 %@", self.expectedVersion];
    self.window.minSize = NSMakeSize(780,560);
    self.window.releasedWhenClosed = NO;
    [self.window setFrameAutosaveName:@"AgentHubMainWindow"];

    NSView *view = self.window.contentView;
    NSButton *reconnect = [NSButton buttonWithTitle:@"重新连接" target:self action:@selector(connect:)];
    reconnect.frame = NSMakeRect(14,800,100,28);
    reconnect.autoresizingMask = NSViewMinYMargin;
    [view addSubview:reconnect];
    self.retry = reconnect;

    self.status = [NSTextField labelWithString:@"正在准备工作台…"];
    self.status.frame = NSMakeRect(130,805,1060,20);
    self.status.autoresizingMask = NSViewMinYMargin|NSViewWidthSizable;
    self.status.font = [NSFont systemFontOfSize:12];
    [view addSubview:self.status];

    WKWebViewConfiguration *config = [WKWebViewConfiguration new];
    config.websiteDataStore = WKWebsiteDataStore.nonPersistentDataStore;
    self.web = [[WKWebView alloc] initWithFrame:NSMakeRect(0,0,1240,790) configuration:config];
    self.web.autoresizingMask = NSViewWidthSizable|NSViewHeightSizable;
    self.web.navigationDelegate = self;
    self.web.UIDelegate = self;
    [view addSubview:self.web];

    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    [self connect:nil];
}
- (void)about:(id)sender {
    NSAlert *alert = [NSAlert new];
    alert.messageText = [NSString stringWithFormat:@"本地 Agent 协同工作台 %@", self.expectedVersion];
    alert.informativeText = @"独立 Mac 窗口 · 多 Agent 并行联动 · 人机交互审改 · 资料上传与挂载 · 受控桌面 UI 自动操控\n\n关闭窗口或退出应用不会暂停任务。请在工作台中暂停相应任务。\n\n使用本机 Python 3 与已配置的 CLI / API / Worker / 桌面辅助功能。";
    [alert runModal];
}
- (void)showFiles:(id)sender {
    [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:self.root]];
}
- (void)openAccessibilitySettings:(id)sender {
    NSURL *url = [NSURL URLWithString:@"x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"];
    [[NSWorkspace sharedWorkspace] openURL:url];
}
- (void)connect:(id)sender {
    if (self.busy) return;
    self.busy = YES; self.spawned = NO; self.attempts = 0;
    self.port = [self preferredPort];
    self.retry.enabled = NO;
    self.status.stringValue = @"正在连接本机服务…";
    [self checkHealth];
}
- (void)fail:(NSString *)message {
    self.busy = NO; self.retry.enabled = YES;
    self.status.stringValue = message;
}
- (BOOL)startService {
    NSString *server = [[[NSBundle mainBundle] resourcePath] stringByAppendingPathComponent:@"server"];
    NSString *launcher = [server stringByAppendingPathComponent:@"launch_background.py"];
    NSTask *task = [NSTask new];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/python3"];
    task.arguments = @[@"-B", launcher, self.root];
    task.currentDirectoryURL = [NSURL fileURLWithPath:server];
    task.standardInput = [NSFileHandle fileHandleWithNullDevice];
    NSString *logPath = [self.root stringByAppendingPathComponent:@"desktop-launch.log"];
    if (![[NSFileManager defaultManager] fileExistsAtPath:logPath]) [[NSFileManager defaultManager] createFileAtPath:logPath contents:nil attributes:nil];
    NSFileHandle *log = [NSFileHandle fileHandleForWritingAtPath:logPath];
    [log seekToEndOfFile];
    task.standardOutput = log ?: [NSFileHandle fileHandleWithNullDevice];
    task.standardError = log ?: [NSFileHandle fileHandleWithNullDevice];
    NSMutableDictionary *env = [[[NSProcessInfo processInfo] environment] mutableCopy];
    [env removeObjectForKey:@"PYTHONHOME"];
    [env removeObjectForKey:@"PYTHONPATH"];
    [env removeObjectForKey:@"__PYVENV_LAUNCHER__"];
    task.environment = env;
    NSError *error;
    if (![task launchAndReturnError:&error]) {
        [self fail:[@"无法启动本机服务：" stringByAppendingString:error.localizedDescription]];
        return NO;
    }
    self.status.stringValue = [NSString stringWithFormat:@"正在启动工作台 v%@ 服务…", self.expectedVersion];
    return YES;
}
- (void)checkHealth {
    self.port = [self preferredPort];
    NSInteger port = self.port;
    NSString *keyPath = [self.root stringByAppendingPathComponent:[NSString stringWithFormat:@".console-auth/%ld.key", (long)port]];
    NSData *key = [NSData dataWithContentsOfFile:keyPath];
    NSString *challenge = [[[NSUUID.UUID.UUIDString lowercaseString] componentsSeparatedByString:@"-"] componentsJoinedByString:@""];
    NSData *nonce = [challenge dataUsingEncoding:NSUTF8StringEncoding];
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CCHmac(kCCHmacAlgSHA256, key.bytes, key.length, nonce.bytes, nonce.length, digest);
    NSMutableString *proof = [NSMutableString string];
    for (NSUInteger i = 0; i < sizeof(digest); i++) [proof appendFormat:@"%02x", digest[i]];
    NSString *healthStr = [NSString stringWithFormat:@"http://127.0.0.1:%ld/api/health?challenge=%@", (long)port, challenge];
    NSURL *url = [NSURL URLWithString:healthStr];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url cachePolicy:NSURLRequestReloadIgnoringLocalCacheData timeoutInterval:2];
    [[[NSURLSession sharedSession] dataTaskWithRequest:request completionHandler:^(NSData *data, NSURLResponse *response, NSError *error) {
        NSDictionary *health = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            BOOL match = (!error && [health isKindOfClass:[NSDictionary class]]
                          && [health[@"app"] isEqual:@"agent_hub"]
                          && key.length > 0 && [health[@"proof"] isEqual:proof]
                          && [health[@"version"] isEqual:self.expectedVersion]);
            if (match) {
                self.busy = NO; self.retry.enabled = YES;
                self.status.stringValue = [NSString stringWithFormat:@"已连接 · 工作台 v%@ (127.0.0.1:%ld) · 退出窗口后任务继续", self.expectedVersion, (long)self.port];
                NSString *pageStr = [NSString stringWithFormat:@"http://127.0.0.1:%ld/", (long)self.port];
                NSMutableURLRequest *page = [NSMutableURLRequest requestWithURL:[NSURL URLWithString:pageStr]];
                [page setValue:[[NSString alloc] initWithData:key encoding:NSUTF8StringEncoding] forHTTPHeaderField:@"X-Agent-Hub-Token"];
                [self.web loadRequest:page];
                return;
            }
            if (!self.spawned) {
                self.spawned = YES;
                if (![self startService]) return;
            }
            self.attempts += 1;
            if (self.attempts >= 30) {
                [self fail:@"服务尚未就绪。可点击“重新连接”，或在任务文件夹查看 desktop-launch.log。"];
                return;
            }
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC/2), dispatch_get_main_queue(), ^{[self checkHealth];});
        });
    }] resume];
}
- (void)webView:(WKWebView *)webView decidePolicyForNavigationAction:(WKNavigationAction *)action decisionHandler:(void (^)(WKNavigationActionPolicy))decisionHandler {
    NSURL *url = action.request.URL;
    BOOL local = [url.scheme isEqual:@"http"] && [url.host isEqual:@"127.0.0.1"] && url.port.integerValue == self.port;
    if (local && [url.path isEqualToString:@"/api/download"]) {
        [[NSWorkspace sharedWorkspace] openURL:url];
        decisionHandler(WKNavigationActionPolicyCancel);
        return;
    }
    if (local) { decisionHandler(WKNavigationActionPolicyAllow); return; }
    if (action.navigationType == WKNavigationTypeLinkActivated && ([url.scheme isEqual:@"https"] || [url.scheme isEqual:@"http"])) {
        [[NSWorkspace sharedWorkspace] openURL:url];
    }
    decisionHandler(WKNavigationActionPolicyCancel);
}
- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    if (error.code != NSURLErrorCancelled) [self fail:@"页面连接中断，请点击“重新连接”。"];
}
- (void)webView:(WKWebView *)webView runOpenPanelWithParameters:(WKOpenPanelParameters *)parameters initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSArray<NSURL *> * _Nullable))completionHandler {
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.canChooseFiles = YES;
    panel.canChooseDirectories = parameters.allowsDirectories;
    panel.allowsMultipleSelection = parameters.allowsMultipleSelection;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) {
        completionHandler(result == NSModalResponseOK ? panel.URLs : nil);
    }];
}
- (void)webView:(WKWebView *)webView runJavaScriptAlertPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(void))completionHandler {
    NSAlert *alert = [NSAlert new];
    alert.messageText = @"本地 Agent 工作台";
    alert.informativeText = message ?: @"";
    [alert addButtonWithTitle:@"确定"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse r) {
        completionHandler();
    }];
}
- (void)webView:(WKWebView *)webView runJavaScriptConfirmPanelWithMessage:(NSString *)message initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL))completionHandler {
    NSAlert *alert = [NSAlert new];
    alert.messageText = @"请确认";
    alert.informativeText = message ?: @"";
    [alert addButtonWithTitle:@"确定"];
    [alert addButtonWithTitle:@"取消"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse r) {
        completionHandler(r == NSAlertFirstButtonReturn);
    }];
}
- (void)webView:(WKWebView *)webView runJavaScriptTextInputPanelWithPrompt:(NSString *)prompt defaultText:(NSString *)defaultText initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSString * _Nullable))completionHandler {
    NSAlert *alert = [NSAlert new];
    alert.messageText = @"请输入";
    alert.informativeText = prompt ?: @"";
    NSTextField *input = [[NSTextField alloc] initWithFrame:NSMakeRect(0, 0, 320, 24)];
    input.stringValue = defaultText ?: @"";
    alert.accessoryView = input;
    [alert addButtonWithTitle:@"确定"];
    [alert addButtonWithTitle:@"取消"];
    [alert beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse r) {
        completionHandler(r == NSAlertFirstButtonReturn ? input.stringValue : nil);
    }];
}
- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    [self.window makeKeyAndOrderFront:nil];
    [self connect:nil];
    return YES;
}
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { return YES; }
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        HubApp *delegate = [HubApp new];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}
