#include <libproc.h>
#include <sys/resource.h>
#include <stdio.h>
#include <unistd.h>
#include <stdint.h>
static void snap(const char*tag, rusage_info_current *r){
  if(proc_pid_rusage(getpid(), RUSAGE_INFO_CURRENT, (rusage_info_t*)r)!=0){perror("proc_pid_rusage");return;}
  printf("%s: user_ns=%llu instr=%llu cycles=%llu energy_nj=%llu penergy_nj=%llu pinstr=%llu billed=%llu\n",tag,
   r->ri_user_time,r->ri_instructions,r->ri_cycles,r->ri_energy_nj,r->ri_penergy_nj,r->ri_pinstructions,r->ri_billed_energy);
}
int main(){ rusage_info_current a,b; snap("before",&a);
  volatile double x=0; for(long i=0;i<400000000;i++) x+=i*1e-9;
  snap("after",&b);
  printf("delta energy = %.3f J, delta instr = %.3e, delta cycles=%.3e\n",(b.ri_energy_nj-a.ri_energy_nj)/1e9,(double)(b.ri_instructions-a.ri_instructions),(double)(b.ri_cycles-a.ri_cycles));
  return 0;}
