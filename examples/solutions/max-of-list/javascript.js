const [n, ...nums] = require("fs").readFileSync(0, "utf8").trim().split(/\s+/).map(Number);
console.log(Math.max(...nums.slice(0, n)));
