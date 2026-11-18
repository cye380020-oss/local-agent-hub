#import <Cocoa/Cocoa.h>
static void emit(NSDictionary *value) {
    NSData *data=[NSJSONSerialization dataWithJSONObject:value options:0 error:nil];
    fwrite(data.bytes,1,data.length,stdout);fputc('\n',stdout);fflush(stdout);
}
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if(argc!=3){emit(@{@"started":@NO,@"error":@"应用参数不完整"});return 1;}
        NSString *path=[[NSString stringWithUTF8String:argv[1]] stringByResolvingSymlinksInPath];
        NSString *expected=[NSString stringWithUTF8String:argv[2]];
        NSBundle *bundle=[NSBundle bundleWithPath:path];
        if(![path.pathExtension isEqual:@"app"] || ![bundle.bundleIdentifier isEqual:expected]){
            emit(@{@"started":@NO,@"error":@"应用身份与配置不符"});return 1;
        }
        __block BOOL done=NO;
        __block NSDictionary *result;
        NSWorkspaceOpenConfiguration *config=[NSWorkspaceOpenConfiguration configuration];
        config.activates=YES;
        config.createsNewApplicationInstance=NO;
        [[NSWorkspace sharedWorkspace] openApplicationAtURL:[NSURL fileURLWithPath:path] configuration:config completionHandler:^(NSRunningApplication *app,NSError *error){
            BOOL valid=app && !app.terminated && [app.bundleIdentifier isEqual:expected] && [[app.bundleURL.path stringByResolvingSymlinksInPath] isEqual:path];
            result=valid?@{@"started":@YES,@"pid":@(app.processIdentifier),@"bundle_id":expected,@"app_path":path}:@{@"started":@NO,@"error":@"macOS 未确认应用启动，请检查是否等待系统提示或登录"};
            done=YES;
        }];
        NSDate *deadline=[NSDate dateWithTimeIntervalSinceNow:12];
        while(!done && deadline.timeIntervalSinceNow>0){[[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];}
        emit(result?:@{@"started":@NO,@"error":@"启动确认超时，请检查应用或系统提示后重试"});
        return [result[@"started"] boolValue]?0:1;
    }
}
