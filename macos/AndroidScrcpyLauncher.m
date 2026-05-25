#import <Foundation/Foundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static NSString *DefaultScrcpyPath(void) {
    NSDictionary *env = [[NSProcessInfo processInfo] environment];
    NSString *configured = env[@"ANDROID_USE_SCRCPY"] ?: env[@"ANDROIDS_SCRCPY"];
    if (configured.length > 0) {
        return configured;
    }
    NSArray<NSString *> *candidates = @[
        @"/opt/homebrew/bin/scrcpy",
        @"/usr/local/bin/scrcpy",
        @"/usr/bin/scrcpy",
        @"scrcpy",
    ];
    for (NSString *candidate in candidates) {
        if ([candidate isEqualToString:@"scrcpy"]) {
            return candidate;
        }
        if ([[NSFileManager defaultManager] isExecutableFileAtPath:candidate]) {
            return candidate;
        }
    }
    return @"scrcpy";
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSMutableArray<NSString *> *arguments = [NSMutableArray array];
        NSString *scrcpyPath = DefaultScrcpyPath();

        for (int i = 1; i < argc; i++) {
            NSString *arg = [NSString stringWithUTF8String:argv[i]];
            if ([arg isEqualToString:@"--scrcpy"] && i + 1 < argc) {
                scrcpyPath = [NSString stringWithUTF8String:argv[++i]];
                continue;
            }
            [arguments addObject:arg];
        }

        NSUInteger count = arguments.count + 2;
        char **execArgs = calloc(count, sizeof(char *));
        if (!execArgs) {
            return 127;
        }
        execArgs[0] = strdup([scrcpyPath fileSystemRepresentation]);
        for (NSUInteger i = 0; i < arguments.count; i++) {
            execArgs[i + 1] = strdup([arguments[i] UTF8String]);
        }
        execArgs[count - 1] = NULL;

        execvp(execArgs[0], execArgs);
        perror("execvp scrcpy");
        return 127;
    }
}
