#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>

static void emit(NSDictionary *value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:0 error:nil];
    if (data) {
        fwrite(data.bytes, 1, data.length, stdout);
    }
    fputc('\n', stdout);
    fflush(stdout);
}

static BOOL isScreenLocked(void) {
    CFDictionaryRef dict = CGSessionCopyCurrentDictionary();
    if (!dict) return NO;
    BOOL locked = NO;
    CFBooleanRef val = (CFBooleanRef)CFDictionaryGetValue(dict, CFSTR("CGSSessionScreenIsLocked"));
    if (val && CFGetTypeID(val) == CFBooleanGetTypeID()) {
        locked = CFBooleanGetValue(val);
    }
    CFRelease(dict);
    return locked;
}

static void collectAXText(AXUIElementRef elem, NSMutableArray<NSString *> *out, int depth, int *nodes) {
    if (!elem || depth > 14 || *nodes > 600) return;
    *nodes += 1;
    CFTypeRef valRef = NULL;
    if (AXUIElementCopyAttributeValue(elem, kAXValueAttribute, &valRef) == kAXErrorSuccess && valRef) {
        if (CFGetTypeID(valRef) == CFStringGetTypeID()) {
            NSString *s = [(__bridge NSString *)valRef stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
            if (s.length > 0 && s.length < 200000) [out addObject:s];
        }
        CFRelease(valRef);
    }
    CFTypeRef titleRef = NULL;
    if (AXUIElementCopyAttributeValue(elem, kAXTitleAttribute, &titleRef) == kAXErrorSuccess && titleRef) {
        if (CFGetTypeID(titleRef) == CFStringGetTypeID()) {
            NSString *s = [(__bridge NSString *)titleRef stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
            if (s.length > 0 && s.length < 4000) [out addObject:s];
        }
        CFRelease(titleRef);
    }
    CFTypeRef childrenRef = NULL;
    if (AXUIElementCopyAttributeValue(elem, kAXChildrenAttribute, &childrenRef) == kAXErrorSuccess && childrenRef) {
        if (CFGetTypeID(childrenRef) == CFArrayGetTypeID()) {
            CFArrayRef arr = (CFArrayRef)childrenRef;
            CFIndex count = MIN(CFArrayGetCount(arr), (CFIndex)120);
            for (CFIndex i = 0; i < count && *nodes <= 600; i++) {
                AXUIElementRef child = (AXUIElementRef)CFArrayGetValueAtIndex(arr, i);
                collectAXText(child, out, depth + 1, nodes);
            }
        }
        CFRelease(childrenRef);
    }
}

static void sendKeyCombo(CGKeyCode key, CGEventFlags flags) {
    CGEventSourceRef src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState);
    CGEventRef down = CGEventCreateKeyboardEvent(src, key, true);
    CGEventRef up = CGEventCreateKeyboardEvent(src, key, false);
    if (flags) {
        CGEventSetFlags(down, flags);
        CGEventSetFlags(up, flags);
    }
    CGEventPost(kCGHIDEventTap, down);
    usleep(35000);
    CGEventPost(kCGHIDEventTap, up);
    CFRelease(down);
    CFRelease(up);
    if (src) CFRelease(src);
}

static void triggerShortcut(NSString *spec) {
    NSString *s = [spec lowercaseString];
    if ([s isEqual:@"none"] || s.length == 0) return;
    if ([s isEqual:@"cmd+n"]) { sendKeyCombo(45, kCGEventFlagMaskCommand); return; }
    if ([s isEqual:@"cmd+k"]) { sendKeyCombo(40, kCGEventFlagMaskCommand); return; }
    if ([s isEqual:@"cmd+shift+n"]) { sendKeyCombo(45, kCGEventFlagMaskCommand | kCGEventFlagMaskShift); return; }
    if ([s isEqual:@"cmd+l"]) { sendKeyCombo(37, kCGEventFlagMaskCommand); return; }
    if ([s isEqual:@"cmd+return"]) { sendKeyCombo(36, kCGEventFlagMaskCommand); return; }
    if ([s isEqual:@"return"]) { sendKeyCombo(36, 0); return; }
    if ([s isEqual:@"cmd+v"]) { sendKeyCombo(9, kCGEventFlagMaskCommand); return; }
    if ([s isEqual:@"cmd+c"]) { sendKeyCombo(8, kCGEventFlagMaskCommand); return; }
}

static NSRunningApplication *findRunning(NSString *bundleId) {
    NSArray<NSRunningApplication *> *list = [NSRunningApplication runningApplicationsWithBundleIdentifier:bundleId];
    for (NSRunningApplication *app in list) {
        if (!app.terminated) return app;
    }
    return nil;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            emit(@{@"ok": @NO, @"error": @"缺少子命令（check / read / send）"});
            return 1;
        }
        NSString *cmd = [NSString stringWithUTF8String:argv[1]];
        if ([cmd isEqual:@"check"]) {
            BOOL prompt = (argc >= 3 && [[NSString stringWithUTF8String:argv[2]] isEqual:@"--prompt"]);
            NSDictionary *opts = @{(__bridge id)kAXTrustedCheckOptionPrompt: @(prompt)};
            BOOL trusted = AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)opts);
            emit(@{@"ok": @YES, @"trusted": @(trusted), @"screen_locked": @(isScreenLocked())});
            return 0;
        }
        if (!AXIsProcessTrusted()) {
            emit(@{@"ok": @NO, @"error": @"未授予 macOS 辅助功能权限；请在“系统设置 → 隐私与安全性 → 辅助功能”中授权后再试", @"trusted": @NO});
            return 2;
        }
        if (isScreenLocked()) {
            emit(@{@"ok": @NO, @"error": @"Mac 当前处于锁屏状态，无法执行桌面界面操作", @"screen_locked": @YES});
            return 3;
        }
        if ([cmd isEqual:@"read"]) {
            if (argc < 3) { emit(@{@"ok": @NO, @"error": @"缺少 bundle_id"}); return 1; }
            NSString *bid = [NSString stringWithUTF8String:argv[2]];
            NSRunningApplication *app = findRunning(bid);
            if (!app) { emit(@{@"ok": @NO, @"error": @"目标应用未运行"}); return 1; }
            AXUIElementRef appElem = AXUIElementCreateApplication(app.processIdentifier);
            AXUIElementRef win = NULL;
            AXError err = AXUIElementCopyAttributeValue(appElem, kAXFocusedWindowAttribute, (CFTypeRef *)&win);
            if (err != kAXErrorSuccess || !win) {
                CFTypeRef winsRef = NULL;
                if (AXUIElementCopyAttributeValue(appElem, kAXWindowsAttribute, &winsRef) == kAXErrorSuccess && winsRef) {
                    if (CFGetTypeID(winsRef) == CFArrayGetTypeID() && CFArrayGetCount((CFArrayRef)winsRef) > 0) {
                        win = (AXUIElementRef)CFRetain(CFArrayGetValueAtIndex((CFArrayRef)winsRef, 0));
                    }
                    CFRelease(winsRef);
                }
            }
            if (!win) {
                CFRelease(appElem);
                emit(@{@"ok": @NO, @"error": @"无法读取目标应用窗口（可能已最小化或无可见窗口）"});
                return 1;
            }
            NSMutableArray<NSString *> *chunks = [NSMutableArray array];
            int nodes = 0;
            collectAXText(win, chunks, 0, &nodes);
            CFRelease(win);
            CFRelease(appElem);
            NSString *joined = [chunks componentsJoinedByString:@"\n"];
            if (joined.length > 300000) {
                joined = [joined substringFromIndex:joined.length - 300000];
            }
            emit(@{@"ok": @YES, @"pid": @(app.processIdentifier), @"nodes": @(nodes), @"text": joined});
            return 0;
        }
        if ([cmd isEqual:@"send"]) {
            if (argc < 6) {
                emit(@{@"ok": @NO, @"error": @"send 参数不足：app_path bundle_id new_chat_shortcut send_shortcut"});
                return 1;
            }
            NSString *path = [[NSString stringWithUTF8String:argv[2]] stringByResolvingSymlinksInPath];
            NSString *bid = [NSString stringWithUTF8String:argv[3]];
            NSString *newShortcut = [NSString stringWithUTF8String:argv[4]];
            NSString *sendShortcut = [NSString stringWithUTF8String:argv[5]];
            NSBundle *bundle = [NSBundle bundleWithPath:path];
            if (![path.pathExtension isEqual:@"app"] || ![bundle.bundleIdentifier isEqual:bid]) {
                emit(@{@"ok": @NO, @"error": @"目标应用路径与 Bundle ID 不匹配"});
                return 1;
            }
            NSData *stdinData = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
            NSString *prompt = [[NSString alloc] initWithData:stdinData encoding:NSUTF8StringEncoding];
            if (!prompt || prompt.length == 0) {
                emit(@{@"ok": @NO, @"error": @"任务提示为空"});
                return 1;
            }
            __block BOOL opened = NO;
            __block NSRunningApplication *running = nil;
            NSWorkspaceOpenConfiguration *cfg = [NSWorkspaceOpenConfiguration configuration];
            cfg.activates = YES;
            cfg.createsNewApplicationInstance = NO;
            [[NSWorkspace sharedWorkspace] openApplicationAtURL:[NSURL fileURLWithPath:path] configuration:cfg completionHandler:^(NSRunningApplication *app, NSError *error) {
                running = app;
                opened = YES;
            }];
            NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:10];
            while (!opened && deadline.timeIntervalSinceNow > 0) {
                [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
            }
            if (!running || running.terminated || ![running.bundleIdentifier isEqual:bid]) {
                emit(@{@"ok": @NO, @"error": @"无法激活目标桌面应用"});
                return 1;
            }
            [running activateWithOptions:NSApplicationActivateIgnoringOtherApps];
            usleep(450000);
            if (newShortcut.length > 0 && ![newShortcut isEqual:@"none"]) {
                triggerShortcut(newShortcut);
                usleep(500000);
            }
            NSPasteboard *pb = [NSPasteboard generalPasteboard];
            NSString *savedClipboard = [pb stringForType:NSPasteboardTypeString];
            [pb clearContents];
            [pb setString:prompt forType:NSPasteboardTypeString];
            usleep(120000);
            triggerShortcut(@"cmd+v");
            usleep(220000);
            triggerShortcut(sendShortcut.length > 0 ? sendShortcut : @"return");
            usleep(250000);
            if (savedClipboard) {
                [pb clearContents];
                [pb setString:savedClipboard forType:NSPasteboardTypeString];
            }
            emit(@{@"ok": @YES, @"pid": @(running.processIdentifier), @"bundle_id": bid, @"app_path": path});
            return 0;
        }
        emit(@{@"ok": @NO, @"error": @"未知子命令"});
        return 1;
    }
}
