#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <unistd.h>
#include <stdint.h>
typedef struct IOReportSubscriptionRef* IOReportSubscriptionRef;
extern CFMutableDictionaryRef IOReportCopyChannelsInGroup(CFStringRef, CFStringRef, uint64_t, uint64_t, uint64_t);
extern IOReportSubscriptionRef IOReportCreateSubscription(void*, CFMutableDictionaryRef, CFMutableDictionaryRef*, uint64_t, CFTypeRef);
extern CFDictionaryRef IOReportCreateSamples(IOReportSubscriptionRef, CFMutableDictionaryRef, CFTypeRef);
extern CFDictionaryRef IOReportCreateSamplesDelta(CFDictionaryRef, CFDictionaryRef, CFTypeRef);
extern CFStringRef IOReportChannelGetChannelName(CFDictionaryRef);
extern CFStringRef IOReportChannelGetUnitLabel(CFDictionaryRef);
extern int64_t IOReportSimpleGetIntegerValue(CFDictionaryRef, int32_t);
static void busy(double sec){ volatile double x=0; CFAbsoluteTime t0=CFAbsoluteTimeGetCurrent(); while(CFAbsoluteTimeGetCurrent()-t0<sec) for(int i=0;i<100000;i++) x+=i*1e-9; }
int main(int argc,char**argv){
  CFMutableDictionaryRef ch = IOReportCopyChannelsInGroup(CFSTR("Energy Model"), NULL, 0,0,0);
  if(!ch){printf("no channels\n");return 1;}
  CFMutableDictionaryRef subbed=NULL;
  IOReportSubscriptionRef sub = IOReportCreateSubscription(NULL, ch, &subbed, 0, NULL);
  if(!sub){printf("no subscription\n");return 1;}
  CFDictionaryRef a = IOReportCreateSamples(sub, subbed, NULL);
  int mode = argc>1 ? atoi(argv[1]) : 0;
  if(mode==1) busy(2.0); else sleep(2);
  CFDictionaryRef b = IOReportCreateSamples(sub, subbed, NULL);
  CFDictionaryRef d = IOReportCreateSamplesDelta(a,b,NULL);
  CFArrayRef arr = CFDictionaryGetValue(d, CFSTR("IOReportChannels"));
  for(CFIndex i=0;i<CFArrayGetCount(arr);i++){
    CFDictionaryRef c = CFArrayGetValueAtIndex(arr,i);
    char name[128]="",unit[32]="";
    CFStringRef n=IOReportChannelGetChannelName(c), u=IOReportChannelGetUnitLabel(c);
    if(n) CFStringGetCString(n,name,sizeof name,kCFStringEncodingUTF8);
    if(u) CFStringGetCString(u,unit,sizeof unit,kCFStringEncodingUTF8);
    int64_t v=IOReportSimpleGetIntegerValue(c,0);
    if(1) printf("%-24s %12lld %s\n",name,(long long)v,unit);
  }
  return 0;}
