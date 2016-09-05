/*
 * Constants used for GPS time detection and rollover correction.
 *
 * Correct for week beginning 2016-09-01T00:00:00
 */
#define BUILD_CENTURY	2000
#define BUILD_WEEK	888                   # Assumes 10-bit week counter
#define BUILD_LEAPSECONDS	17
#define BUILD_ROLLOVERS	1         # Assumes 10-bit week counter
