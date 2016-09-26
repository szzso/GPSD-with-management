#include <sys/un.h>
int main()
{
#ifndef SUN_LEN
    (void) SUN_LEN;
#endif
    ;
    return 0;
}
