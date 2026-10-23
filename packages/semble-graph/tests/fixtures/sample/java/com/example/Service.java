package com.example;

import com.example.Helper;

public class Service extends Base implements Api {
    private static final int LIMIT = 10;

    public void run() {
        Helper.help();
    }
}
