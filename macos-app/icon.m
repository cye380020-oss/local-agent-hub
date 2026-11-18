#import <Cocoa/Cocoa.h>
int main(int argc, char **argv) { @autoreleasepool {
    NSString *dir = [NSString stringWithUTF8String:argv[1]];
    [[NSFileManager defaultManager] createDirectoryAtPath:dir withIntermediateDirectories:YES attributes:nil error:nil];
    int dims[] = {16,32,128,256,512};
    for(int i=0;i<5;i++) for(int scale=1;scale<=2;scale++) {
        int n=dims[i]*scale;
        NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL pixelsWide:n pixelsHigh:n bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace bytesPerRow:0 bitsPerPixel:0];
        [NSGraphicsContext saveGraphicsState];
        [NSGraphicsContext setCurrentContext:[NSGraphicsContext graphicsContextWithBitmapImageRep:rep]];
        NSAffineTransform *transform = [NSAffineTransform transform]; [transform scaleBy:n/512.0]; [transform concat];
        [[NSColor colorWithRed:.055 green:.082 blue:.14 alpha:1] setFill];
        [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(20,20,472,472) xRadius:108 yRadius:108] fill];
        NSPoint points[] = {NSMakePoint(256,356),NSMakePoint(158,192),NSMakePoint(354,192)};
        [[NSColor colorWithRed:.35 green:.70 blue:.90 alpha:1] setStroke];
        for(int j=0;j<3;j++){NSBezierPath *line=[NSBezierPath bezierPath];[line moveToPoint:points[j]];[line lineToPoint:points[(j+1)%3]];line.lineWidth=20;[line stroke];}
        for(int j=0;j<3;j++){[[NSColor colorWithRed:.67 green:.88 blue:.74 alpha:1] setFill];[[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(points[j].x-43,points[j].y-43,86,86)] fill];}
        [[NSColor whiteColor] setFill];[[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(236,236,40,40)] fill];
        [NSGraphicsContext restoreGraphicsState];
        NSString *name = [NSString stringWithFormat:@"icon_%dx%d%@.png",dims[i],dims[i],scale==2?@"@2x":@""];
        [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:[dir stringByAppendingPathComponent:name] atomically:YES];
    }
} return 0; }
