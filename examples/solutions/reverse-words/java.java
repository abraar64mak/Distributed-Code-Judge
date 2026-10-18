import java.util.*;

public class Main {
    public static void main(String[] args) {
        List<String> words = Arrays.asList(new Scanner(System.in).nextLine().trim().split("\\s+"));
        Collections.reverse(words);
        System.out.println(String.join(" ", words));
    }
}
