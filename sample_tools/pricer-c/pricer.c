/* pricer: applies currency rate + margin to a parts report CSV.
 * build: make
 * run:   ./pricer <in.csv> <out.csv> <rate> [margin_pct]
 * expects column 5 (1-based) = unit_price; appends retail_price column.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: pricer <in.csv> <out.csv> <rate> [margin_pct]\n");
        return 2;
    }
    FILE *in = fopen(argv[1], "r");
    if (!in) { perror("open input"); return 1; }
    FILE *out = fopen(argv[2], "w");
    if (!out) { perror("open output"); return 1; }
    double rate = atof(argv[3]);
    double margin = argc > 4 ? atof(argv[4]) : 20.0;
    char line[4096];
    int first = 1;
    while (fgets(line, sizeof line, in)) {
        line[strcspn(line, "\r\n")] = 0;
        if (first) { fprintf(out, "%s,retail_price\n", line); first = 0; continue; }
        char copy[4096]; strcpy(copy, line);
        char *tok = strtok(copy, ","); int col = 1; double price = 0;
        while (tok) { if (col == 5) price = atof(tok); tok = strtok(NULL, ","); col++; }
        fprintf(out, "%s,%.2f\n", line, price * rate * (1.0 + margin / 100.0));
    }
    fclose(in); fclose(out);
    return 0;
}
