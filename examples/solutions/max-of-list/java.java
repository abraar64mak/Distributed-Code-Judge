import java.util.Scanner;

public class Main {
    public static void main(String[] args) {
        Scanner in = new Scanner(System.in);
        int n = in.nextInt();
        long best = Long.MIN_VALUE;
        for (int i = 0; i < n; i++) best = Math.max(best, in.nextLong());
        System.out.println(best);
    }
}
